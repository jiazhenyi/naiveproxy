// Copyright (c) 2011 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// See "SSPI Sample Application" at
// http://msdn.microsoft.com/en-us/library/aa918273.aspx
// and "NTLM Security Support Provider" at
// http://msdn.microsoft.com/en-us/library/aa923611.aspx.

#include "net/http/http_auth_handler_ntlm.h"

#include "base/strings/string_util.h"
#include "net/base/net_errors.h"
#include "net/dns/host_resolver.h"
#include "net/http/http_auth.h"
#include "net/http/http_auth_preferences.h"
#include "net/http/http_auth_sspi_win.h"

namespace net {

int HttpAuthHandlerNTLM::Factory::CreateAuthHandler(
    HttpAuthChallengeTokenizer* challenge,
    HttpAuth::Target target,
    const SSLInfo& ssl_info,
    const NetworkIsolationKey& network_isolation_key,
    const GURL& origin,
    CreateReason reason,
    int digest_nonce_count,
    const NetLogWithSource& net_log,
    HostResolver* host_resolver,
    std::unique_ptr<HttpAuthHandler>* handler) {
  if (reason == CREATE_PREEMPTIVE)
    return ERR_UNSUPPORTED_AUTH_SCHEME;
  // TODO(cbentzel): Move towards model of parsing in the factory
  //                 method and only constructing when valid.
  std::unique_ptr<HttpAuthHandler> tmp_handler(
      new HttpAuthHandlerNTLM(sspi_library_.get(), http_auth_preferences()));
  if (!tmp_handler->InitFromChallenge(challenge, target, ssl_info,
                                      network_isolation_key, origin, net_log))
    return ERR_INVALID_RESPONSE;
  handler->swap(tmp_handler);
  return OK;
}

HttpAuthHandlerNTLM::HttpAuthHandlerNTLM(
    SSPILibrary* sspi_library,
    const HttpAuthPreferences* http_auth_preferences)
    : mechanism_(sspi_library, HttpAuth::AUTH_SCHEME_NTLM),
      http_auth_preferences_(http_auth_preferences) {}

int HttpAuthHandlerNTLM::GenerateAuthTokenImpl(
    const AuthCredentials* credentials,
    const HttpRequestInfo* request,
    CompletionOnceCallback callback,
    std::string* auth_token) {
  return mechanism_.GenerateAuthToken(credentials, CreateSPN(origin_),
                                      channel_bindings_, auth_token, net_log(),
                                      std::move(callback));
}

HttpAuthHandlerNTLM::~HttpAuthHandlerNTLM() {}

// Require identity on first pass instead of second.
bool HttpAuthHandlerNTLM::NeedsIdentity() {
  return mechanism_.NeedsIdentity();
}

bool HttpAuthHandlerNTLM::AllowsDefaultCredentials() {
  if (target_ == HttpAuth::AUTH_PROXY)
    return true;
  if (!http_auth_preferences_)
    return false;
  return http_auth_preferences_->CanUseDefaultCredentials(origin_);
}

HttpAuth::AuthorizationResult HttpAuthHandlerNTLM::ParseChallenge(
    HttpAuthChallengeTokenizer* tok) {
  return mechanism_.ParseChallenge(tok);
}

}  // namespace net
