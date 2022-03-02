// Copyright (c) 2010 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "net/http/http_auth_handler_factory.h"

#include <set>

#include "base/containers/contains.h"
#include "base/memory/ptr_util.h"
#include "base/strings/string_util.h"
#include "build/build_config.h"
#include "net/base/net_errors.h"
#include "net/dns/host_resolver.h"
#include "net/http/http_auth_challenge_tokenizer.h"
#include "net/http/http_auth_filter.h"
#include "net/http/http_auth_handler_basic.h"
#include "net/http/http_auth_handler_digest.h"
#include "net/http/http_auth_handler_ntlm.h"
#include "net/http/http_auth_preferences.h"
#include "net/http/http_auth_scheme.h"
#include "net/log/net_log_values.h"
#include "net/net_buildflags.h"
#include "net/ssl/ssl_info.h"
#include "url/scheme_host_port.h"

#if BUILDFLAG(USE_KERBEROS)
#include "net/http/http_auth_handler_negotiate.h"
#endif

namespace {

base::Value NetLogParamsForCreateAuth(
    const std::string& scheme,
    const std::string& challenge,
    const int net_error,
    const url::SchemeHostPort& scheme_host_port,
    const absl::optional<bool>& allows_default_credentials,
    net::NetLogCaptureMode capture_mode) {
  base::Value dict(base::Value::Type::DICTIONARY);
  dict.SetKey("scheme", net::NetLogStringValue(scheme));
  if (net::NetLogCaptureIncludesSensitive(capture_mode))
    dict.SetKey("challenge", net::NetLogStringValue(challenge));
  dict.SetStringKey("origin", scheme_host_port.Serialize());
  if (allows_default_credentials)
    dict.SetBoolKey("allows_default_credentials", *allows_default_credentials);
  if (net_error < 0)
    dict.SetIntKey("net_error", net_error);
  return dict;
}

}  // namespace

namespace net {

int HttpAuthHandlerFactory::CreateAuthHandlerFromString(
    const std::string& challenge,
    HttpAuth::Target target,
    const SSLInfo& ssl_info,
    const NetworkIsolationKey& network_isolation_key,
    const url::SchemeHostPort& scheme_host_port,
    const NetLogWithSource& net_log,
    HostResolver* host_resolver,
    std::unique_ptr<HttpAuthHandler>* handler) {
  HttpAuthChallengeTokenizer props(challenge.begin(), challenge.end());
  return CreateAuthHandler(&props, target, ssl_info, network_isolation_key,
                           scheme_host_port, CREATE_CHALLENGE, 1, net_log,
                           host_resolver, handler);
}

int HttpAuthHandlerFactory::CreatePreemptiveAuthHandlerFromString(
    const std::string& challenge,
    HttpAuth::Target target,
    const NetworkIsolationKey& network_isolation_key,
    const url::SchemeHostPort& scheme_host_port,
    int digest_nonce_count,
    const NetLogWithSource& net_log,
    HostResolver* host_resolver,
    std::unique_ptr<HttpAuthHandler>* handler) {
  HttpAuthChallengeTokenizer props(challenge.begin(), challenge.end());
  SSLInfo null_ssl_info;
  return CreateAuthHandler(&props, target, null_ssl_info, network_isolation_key,
                           scheme_host_port, CREATE_PREEMPTIVE,
                           digest_nonce_count, net_log, host_resolver, handler);
}

HttpAuthHandlerRegistryFactory::HttpAuthHandlerRegistryFactory(
    const HttpAuthPreferences* http_auth_preferences) {
  set_http_auth_preferences(http_auth_preferences);
}

HttpAuthHandlerRegistryFactory::~HttpAuthHandlerRegistryFactory() = default;

void HttpAuthHandlerRegistryFactory::SetHttpAuthPreferences(
    const std::string& scheme,
    const HttpAuthPreferences* prefs) {
  HttpAuthHandlerFactory* factory = GetRegisteredSchemeFactory(scheme);
  if (factory)
    factory->set_http_auth_preferences(prefs);
}

void HttpAuthHandlerRegistryFactory::RegisterSchemeFactory(
    const std::string& scheme,
    HttpAuthHandlerFactory* factory) {
  std::string lower_scheme = base::ToLowerASCII(scheme);
  if (factory) {
    factory->set_http_auth_preferences(http_auth_preferences());
    factory_map_[lower_scheme] = base::WrapUnique(factory);
  } else {
    factory_map_.erase(lower_scheme);
  }
}

HttpAuthHandlerFactory* HttpAuthHandlerRegistryFactory::GetSchemeFactory(
    const std::string& scheme) const {
  std::string lower_scheme = base::ToLowerASCII(scheme);
  const auto& allowed_schemes = GetAllowedAuthSchemes();
  if (allowed_schemes.find(lower_scheme) == allowed_schemes.end()) {
    return nullptr;
  }
  return GetRegisteredSchemeFactory(scheme);
}

// static
std::unique_ptr<HttpAuthHandlerRegistryFactory>
HttpAuthHandlerFactory::CreateDefault(
    const HttpAuthPreferences* prefs
#if BUILDFLAG(USE_EXTERNAL_GSSAPI)
    ,
    const std::string& gssapi_library_name
#endif
#if BUILDFLAG(USE_KERBEROS)
    ,
    HttpAuthMechanismFactory negotiate_auth_system_factory
#endif
) {
  return HttpAuthHandlerRegistryFactory::Create(prefs
#if BUILDFLAG(USE_EXTERNAL_GSSAPI)
                                                ,
                                                gssapi_library_name
#endif
#if BUILDFLAG(USE_KERBEROS)
                                                ,
                                                negotiate_auth_system_factory
#endif
  );
}

// static
std::unique_ptr<HttpAuthHandlerRegistryFactory>
HttpAuthHandlerRegistryFactory::Create(
    const HttpAuthPreferences* prefs
#if BUILDFLAG(USE_EXTERNAL_GSSAPI)
    ,
    const std::string& gssapi_library_name
#endif
#if BUILDFLAG(USE_KERBEROS)
    ,
    HttpAuthMechanismFactory negotiate_auth_system_factory
#endif
) {
  std::unique_ptr<HttpAuthHandlerRegistryFactory> registry_factory(
      new HttpAuthHandlerRegistryFactory(prefs));

  registry_factory->RegisterSchemeFactory(kBasicAuthScheme,
                                          new HttpAuthHandlerBasic::Factory());

  registry_factory->RegisterSchemeFactory(kDigestAuthScheme,
                                          new HttpAuthHandlerDigest::Factory());

  HttpAuthHandlerNTLM::Factory* ntlm_factory =
      new HttpAuthHandlerNTLM::Factory();
#if BUILDFLAG(IS_WIN)
  ntlm_factory->set_sspi_library(
      std::make_unique<SSPILibraryDefault>(NTLMSP_NAME));
#endif  // BUILDFLAG(IS_WIN)
  registry_factory->RegisterSchemeFactory(kNtlmAuthScheme, ntlm_factory);

#if BUILDFLAG(USE_KERBEROS)
  HttpAuthHandlerNegotiate::Factory* negotiate_factory =
      new HttpAuthHandlerNegotiate::Factory(negotiate_auth_system_factory);
#if BUILDFLAG(IS_WIN)
  negotiate_factory->set_library(
      std::make_unique<SSPILibraryDefault>(NEGOSSP_NAME));
#elif BUILDFLAG(USE_EXTERNAL_GSSAPI)
  negotiate_factory->set_library(
      std::make_unique<GSSAPISharedLibrary>(gssapi_library_name));
#endif
  registry_factory->RegisterSchemeFactory(kNegotiateAuthScheme,
                                          negotiate_factory);
#endif  // BUILDFLAG(USE_KERBEROS)

  if (prefs) {
    registry_factory->set_http_auth_preferences(prefs);
    for (auto& factory_entry : registry_factory->factory_map_) {
      factory_entry.second->set_http_auth_preferences(prefs);
    }
  }
  return registry_factory;
}

int HttpAuthHandlerRegistryFactory::CreateAuthHandler(
    HttpAuthChallengeTokenizer* challenge,
    HttpAuth::Target target,
    const SSLInfo& ssl_info,
    const NetworkIsolationKey& network_isolation_key,
    const url::SchemeHostPort& scheme_host_port,
    CreateReason reason,
    int digest_nonce_count,
    const NetLogWithSource& net_log,
    HostResolver* host_resolver,
    std::unique_ptr<HttpAuthHandler>* handler) {
  auto scheme = challenge->auth_scheme();

  int net_error;

  if (scheme.empty()) {
    handler->reset();
    net_error = ERR_INVALID_RESPONSE;
  } else {
    auto* factory = GetSchemeFactory(scheme);
    if (!factory) {
      handler->reset();
      net_error = ERR_UNSUPPORTED_AUTH_SCHEME;
    } else {
      net_error = factory->CreateAuthHandler(
          challenge, target, ssl_info, network_isolation_key, scheme_host_port,
          reason, digest_nonce_count, net_log, host_resolver, handler);
    }
  }

  net_log.AddEvent(
      NetLogEventType::AUTH_HANDLER_CREATE_RESULT,
      [&](NetLogCaptureMode capture_mode) {
        return NetLogParamsForCreateAuth(
            scheme, challenge->challenge_text(), net_error, scheme_host_port,
            *handler
                ? absl::make_optional((*handler)->AllowsDefaultCredentials())
                : absl::nullopt,
            capture_mode);
      });
  return net_error;
}

const std::set<std::string>&
HttpAuthHandlerRegistryFactory::GetAllowedAuthSchemes() const {
  if (http_auth_preferences() &&
      http_auth_preferences()->allowed_schemes().has_value()) {
    return *http_auth_preferences()->allowed_schemes();
  }
  return default_auth_schemes_;
}

HttpAuthHandlerFactory*
HttpAuthHandlerRegistryFactory::GetRegisteredSchemeFactory(
    const std::string& scheme) const {
  std::string lower_scheme = base::ToLowerASCII(scheme);
  auto it = factory_map_.find(lower_scheme);
  if (it == factory_map_.end()) {
    return nullptr;  // |scheme| is not registered.
  }
  return it->second.get();
}

}  // namespace net
