// Copyright (c) 2012 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "net/base/port_util.h"

#include <limits>
#include <set>

#include "base/containers/fixed_flat_map.h"
#include "base/lazy_instance.h"
#include "base/logging.h"
#include "base/metrics/histogram_functions.h"
#include "base/notreached.h"
#include "base/strings/string_number_conversions.h"
#include "base/strings/string_util.h"
#include "url/url_constants.h"

namespace net {

namespace {

// Records ports newly blocked in https://github.com/whatwg/fetch/pull/1148 for
// "NAT Slipstreaming v2.0" vulnerability, plus 10080, to measure the breakage
// from blocking them. Every other port is logged as kOther to provide a
// baseline. See also https://samy.pl/slipstream/. Ports are logged regardless
// of protocol and whether they are blocked or not.
// TODO(ricea): Remove this in April 2021.
void LogSlipstreamRestrictedPort(int port) {
  // These values are persisted to logs. Entries should not be renumbered and
  // numeric values should never be reused.
  enum class SlipstreamPort {
    kOther = 0,
    k69 = 1,
    k137 = 2,
    k161 = 3,
    k554 = 4,
    k1719 = 5,
    k1720 = 6,
    k1723 = 7,
    k6566 = 8,
    k10080 = 9,
    kMaxValue = k10080,
  };

  constexpr auto kMap = base::MakeFixedFlatMap<int, SlipstreamPort>({
      {69, SlipstreamPort::k69},
      {137, SlipstreamPort::k137},
      {161, SlipstreamPort::k161},
      {554, SlipstreamPort::k554},
      {1719, SlipstreamPort::k1719},
      {1720, SlipstreamPort::k1720},
      {1723, SlipstreamPort::k1723},
      {6566, SlipstreamPort::k6566},
      {10080, SlipstreamPort::k10080},
  });

  auto* it = kMap.find(port);
  SlipstreamPort as_enum =
      it == kMap.end() ? SlipstreamPort::kOther : it->second;
  base::UmaHistogramEnumeration("Net.Port.SlipstreamRestricted", as_enum);
}

// The general list of blocked ports. Will be blocked unless a specific
// protocol overrides it. (Ex: ftp can use port 21)
// When adding a port to the list, consider also adding it to kAllowablePorts,
// below.
const int kRestrictedPorts[] = {
    1,      // tcpmux
    7,      // echo
    9,      // discard
    11,     // systat
    13,     // daytime
    15,     // netstat
    17,     // qotd
    19,     // chargen
    20,     // ftp data
    21,     // ftp access
    22,     // ssh
    23,     // telnet
    25,     // smtp
    37,     // time
    42,     // name
    43,     // nicname
    53,     // domain
    69,     // tftp
    77,     // priv-rjs
    79,     // finger
    87,     // ttylink
    95,     // supdup
    101,    // hostriame
    102,    // iso-tsap
    103,    // gppitnp
    104,    // acr-nema
    109,    // pop2
    110,    // pop3
    111,    // sunrpc
    113,    // auth
    115,    // sftp
    117,    // uucp-path
    119,    // nntp
    123,    // NTP
    135,    // loc-srv /epmap
    137,    // netbios
    139,    // netbios
    143,    // imap2
    161,    // snmp
    179,    // BGP
    389,    // ldap
    427,    // SLP (Also used by Apple Filing Protocol)
    465,    // smtp+ssl
    512,    // print / exec
    513,    // login
    514,    // shell
    515,    // printer
    526,    // tempo
    530,    // courier
    531,    // chat
    532,    // netnews
    540,    // uucp
    548,    // AFP (Apple Filing Protocol)
    554,    // rtsp
    556,    // remotefs
    563,    // nntp+ssl
    587,    // smtp (rfc6409)
    601,    // syslog-conn (rfc3195)
    636,    // ldap+ssl
    993,    // ldap+ssl
    995,    // pop3+ssl
    1719,   // h323gatestat
    1720,   // h323hostcall
    1723,   // pptp
    2049,   // nfs
    3659,   // apple-sasl / PasswordServer
    4045,   // lockd
    5060,   // sip
    5061,   // sips
    6000,   // X11
    6566,   // sane-port
    6665,   // Alternate IRC [Apple addition]
    6666,   // Alternate IRC [Apple addition]
    6667,   // Standard IRC [Apple addition]
    6668,   // Alternate IRC [Apple addition]
    6669,   // Alternate IRC [Apple addition]
    6697,   // IRC + TLS
    10080,  // Amanda
};

base::LazyInstance<std::multiset<int>>::Leaky g_explicitly_allowed_ports =
    LAZY_INSTANCE_INITIALIZER;

// List of ports which are permitted to be reenabled despite being in
// kRestrictedList. When adding an port to this list you should also update the
// enterprise policy to document the fact that the value can be set. Ports
// should only remain in this list for about a year to give time for users to
// migrate off while stopping them from becoming permanent parts of the web
// platform.
constexpr int kAllowablePorts[] = {
    // TODO(https://crbug.com/1199642) Remove port 554 around 2021/10/15.
    554,
    // TODO(https://crbig.com/1210779) Remove port 6566 around 2021/08/12.
    6566,
    // TODO(https://crbug.com/1196846) Remove port 10080 around 2022/04/01.
    10080,
};

int g_scoped_allowable_port = 0;

}  // namespace

bool IsPortValid(int port) {
  return port >= 0 && port <= std::numeric_limits<uint16_t>::max();
}

bool IsWellKnownPort(int port) {
  return port >= 0 && port < 1024;
}

bool IsPortAllowedForScheme(int port, base::StringPiece url_scheme) {
  // Reject invalid ports.
  if (!IsPortValid(port))
    return false;

  LogSlipstreamRestrictedPort(port);

  // Allow explicitly allowed ports for any scheme.
  if (g_explicitly_allowed_ports.Get().count(port) > 0)
    return true;

  // FTP requests are permitted to use port 21.
  if (base::LowerCaseEqualsASCII(url_scheme, url::kFtpScheme) && port == 21) {
    return true;
  }

  // Finally check against the generic list of restricted ports for all
  // schemes.
  for (int restricted_port : kRestrictedPorts) {
    if (restricted_port == port)
      return false;
  }

  return true;
}

size_t GetCountOfExplicitlyAllowedPorts() {
  return g_explicitly_allowed_ports.Get().size();
}

// Specifies a comma separated list of port numbers that should be accepted
// despite bans. If the string is invalid no allowed ports are stored.
void SetExplicitlyAllowedPorts(base::span<const uint16_t> allowed_ports) {
  std::multiset<int> ports(allowed_ports.begin(), allowed_ports.end());
  g_explicitly_allowed_ports.Get() = std::move(ports);
}

ScopedPortException::ScopedPortException(int port) : port_(port) {
  g_explicitly_allowed_ports.Get().insert(port);
}

ScopedPortException::~ScopedPortException() {
  auto it = g_explicitly_allowed_ports.Get().find(port_);
  if (it != g_explicitly_allowed_ports.Get().end())
    g_explicitly_allowed_ports.Get().erase(it);
  else
    NOTREACHED();
}

NET_EXPORT bool IsAllowablePort(int port) {
  for (auto allowable_port : kAllowablePorts) {
    if (port == allowable_port) {
      return true;
    }
  }

  if (port == g_scoped_allowable_port)
    return true;

  return false;
}

ScopedAllowablePortForTesting::ScopedAllowablePortForTesting(int port) {
  DCHECK_EQ(g_scoped_allowable_port, 0);
  g_scoped_allowable_port = port;
}

ScopedAllowablePortForTesting::~ScopedAllowablePortForTesting() {
  g_scoped_allowable_port = 0;
}

}  // namespace net
