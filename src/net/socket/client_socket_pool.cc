// Copyright 2012 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "net/socket/client_socket_pool.h"

#include <memory>
#include <utility>

#include "base/check_op.h"
#include "base/feature_list.h"
#include "base/functional/bind.h"
#include "net/base/features.h"
#include "net/base/host_port_pair.h"
#include "net/base/proxy_chain.h"
#include "net/dns/public/secure_dns_policy.h"
#include "net/http/http_proxy_connect_job.h"
#include "net/log/net_log_event_type.h"
#include "net/log/net_log_with_source.h"
#include "net/socket/connect_job.h"
#include "net/socket/connect_job_factory.h"
#include "net/socket/socks_connect_job.h"
#include "net/socket/ssl_connect_job.h"
#include "net/socket/stream_socket.h"
#include "net/spdy/spdy_session.h"
#include "net/spdy/spdy_session_pool.h"
#include "url/gurl.h"
#include "url/scheme_host_port.h"
#include "url/url_constants.h"

namespace net {

namespace {

// The maximum duration, in seconds, to keep used idle persistent sockets alive.
int64_t g_used_idle_socket_timeout_s = 300;  // 5 minutes

// Invoked by the transport socket pool after host resolution is complete
// to allow the connection to be aborted, if a matching SPDY session can
// be found. Returns OnHostResolutionCallbackResult::kMayBeDeletedAsync if such
// a session is found, as it will post a task that may delete the calling
// ConnectJob. Also returns kMayBeDeletedAsync if there may already be such
// a task posted.
OnHostResolutionCallbackResult OnHostResolution(
    SpdySessionPool* spdy_session_pool,
    const SpdySessionKey& spdy_session_key,
    bool is_for_websockets,
    const HostPortPair& host_port_pair,
    const std::vector<HostResolverEndpointResult>& endpoint_results,
    const std::set<std::string>& aliases) {
  DCHECK(host_port_pair == spdy_session_key.host_port_pair());

  // It is OK to dereference spdy_session_pool, because the
  // ClientSocketPoolManager will be destroyed in the same callback that
  // destroys the SpdySessionPool.
  return spdy_session_pool->OnHostResolutionComplete(
      spdy_session_key, is_for_websockets, endpoint_results, aliases);
}

}  // namespace

ClientSocketPool::SocketParams::SocketParams(
    std::unique_ptr<SSLConfig> ssl_config_for_origin,
    std::unique_ptr<SSLConfig> base_ssl_config_for_proxies)
    : ssl_config_for_origin_(std::move(ssl_config_for_origin)),
      base_ssl_config_for_proxies_(std::move(base_ssl_config_for_proxies)) {}

ClientSocketPool::SocketParams::~SocketParams() = default;

scoped_refptr<ClientSocketPool::SocketParams>
ClientSocketPool::SocketParams::CreateForHttpForTesting() {
  return base::MakeRefCounted<SocketParams>(
      /*ssl_config_for_origin=*/nullptr,
      /*base_ssl_config_for_proxies=*/nullptr);
}

ClientSocketPool::GroupId::GroupId()
    : privacy_mode_(PrivacyMode::PRIVACY_MODE_DISABLED) {}

ClientSocketPool::GroupId::GroupId(
    url::SchemeHostPort destination,
    PrivacyMode privacy_mode,
    NetworkAnonymizationKey network_anonymization_key,
    SecureDnsPolicy secure_dns_policy)
    : destination_(std::move(destination)),
      privacy_mode_(privacy_mode),
      network_anonymization_key_(
          NetworkAnonymizationKey::IsPartitioningEnabled()
              ? std::move(network_anonymization_key)
              : NetworkAnonymizationKey()),
      secure_dns_policy_(secure_dns_policy) {
  DCHECK(destination_.IsValid());

  // ClientSocketPool only expected to be used for HTTP/HTTPS/WS/WSS cases, and
  // "ws"/"wss" schemes should be converted to "http"/"https" equivalent first.
  DCHECK(destination_.scheme() == url::kHttpScheme ||
         destination_.scheme() == url::kHttpsScheme);
}

ClientSocketPool::GroupId::GroupId(const GroupId& group_id) = default;

ClientSocketPool::GroupId::~GroupId() = default;

ClientSocketPool::GroupId& ClientSocketPool::GroupId::operator=(
    const GroupId& group_id) = default;

ClientSocketPool::GroupId& ClientSocketPool::GroupId::operator=(
    GroupId&& group_id) = default;

std::string ClientSocketPool::GroupId::ToString() const {
  std::string result = destination_.Serialize();

  if (privacy_mode_)
    result = "pm/" + result;

  if (NetworkAnonymizationKey::IsPartitioningEnabled()) {
    result += " <";
    result += network_anonymization_key_.ToDebugString();
    result += ">";
  }

  switch (secure_dns_policy_) {
    case SecureDnsPolicy::kAllow:
      break;
    case SecureDnsPolicy::kDisable:
      result = "dsd/" + result;
      break;
    case SecureDnsPolicy::kBootstrap:
      result = "dns_bootstrap/" + result;
      break;
  }

  return result;
}

ClientSocketPool::~ClientSocketPool() = default;

// static
base::TimeDelta ClientSocketPool::used_idle_socket_timeout() {
  return base::Seconds(g_used_idle_socket_timeout_s);
}

// static
void ClientSocketPool::set_used_idle_socket_timeout(base::TimeDelta timeout) {
  DCHECK_GT(timeout.InSeconds(), 0);
  g_used_idle_socket_timeout_s = timeout.InSeconds();
}

ClientSocketPool::ClientSocketPool(
    bool is_for_websockets,
    const CommonConnectJobParams* common_connect_job_params,
    std::unique_ptr<ConnectJobFactory> connect_job_factory)
    : is_for_websockets_(is_for_websockets),
      common_connect_job_params_(common_connect_job_params),
      connect_job_factory_(std::move(connect_job_factory)) {}

void ClientSocketPool::NetLogTcpClientSocketPoolRequestedSocket(
    const NetLogWithSource& net_log,
    const GroupId& group_id) {
  // TODO(eroman): Split out the host and port parameters.
  net_log.AddEvent(NetLogEventType::TCP_CLIENT_SOCKET_POOL_REQUESTED_SOCKET,
                   [&] { return NetLogGroupIdParams(group_id); });
}

base::Value::Dict ClientSocketPool::NetLogGroupIdParams(
    const GroupId& group_id) {
  base::Value::Dict event_params;
  event_params.Set("group_id", group_id.ToString());
  return event_params;
}

std::unique_ptr<ConnectJob> ClientSocketPool::CreateConnectJob(
    GroupId group_id,
    scoped_refptr<SocketParams> socket_params,
    const ProxyChain& proxy_chain,
    const absl::optional<NetworkTrafficAnnotationTag>& proxy_annotation_tag,
    RequestPriority request_priority,
    SocketTag socket_tag,
    ConnectJob::Delegate* delegate) {
  bool using_ssl = GURL::SchemeIsCryptographic(group_id.destination().scheme());

  // If applicable, set up a callback to handle checking for H2 IP pooling
  // opportunities. We don't perform H2 IP pooling to or through proxy servers,
  // so ignore those cases.
  OnHostResolutionCallback resolution_callback;
  if (using_ssl && proxy_chain.is_direct()) {
    resolution_callback = base::BindRepeating(
        &OnHostResolution, common_connect_job_params_->spdy_session_pool,
        // TODO(crbug.com/1206799): Pass along as SchemeHostPort.
        SpdySessionKey(HostPortPair::FromSchemeHostPort(group_id.destination()),
                       proxy_chain, group_id.privacy_mode(),
                       SpdySessionKey::IsProxySession::kFalse, socket_tag,
                       group_id.network_anonymization_key(),
                       group_id.secure_dns_policy()),
        is_for_websockets_);
  }

  return connect_job_factory_->CreateConnectJob(
      group_id.destination(), proxy_chain, proxy_annotation_tag,
      socket_params->ssl_config_for_origin(),
      socket_params->base_ssl_config_for_proxies(), is_for_websockets_,
      group_id.privacy_mode(), resolution_callback, request_priority,
      socket_tag, group_id.network_anonymization_key(),
      group_id.secure_dns_policy(), common_connect_job_params_, delegate);
}

}  // namespace net
