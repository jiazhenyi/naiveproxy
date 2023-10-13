// Copyright 2014 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// This file is auto-generated by build_utf8_validator_tables.
// DO NOT EDIT.

#include "base/i18n/utf8_validator_tables.h"

#include <iterator>

namespace base {
namespace internal {

const uint8_t kUtf8ValidatorTables[] = {
    // State 0, offset 0x00
    0x00, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x08
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x10
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x18
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x20
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x28
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x30
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x38
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x40
    0x81, 0x81, 0x81, 0x83, 0x83, 0x83, 0x83, 0x83,  // 0x48
    0x83, 0x83, 0x83, 0x83, 0x83, 0x83, 0x83, 0x83,  // 0x50
    0x83, 0x83, 0x83, 0x83, 0x83, 0x83, 0x83, 0x83,  // 0x58
    0x83, 0x83, 0x83, 0x83, 0x83, 0x83, 0x83, 0x83,  // 0x60
    0x83, 0x86, 0x8b, 0x8b, 0x8b, 0x8b, 0x8b, 0x8b,  // 0x68
    0x8b, 0x8b, 0x8b, 0x8b, 0x8b, 0x8b, 0x8e, 0x8b,  // 0x70
    0x8b, 0x93, 0x9c, 0x9c, 0x9c, 0x9f, 0x81, 0x81,  // 0x78
    0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0x80
    0x81,                                            // 0x81
    // State 1, offset 0x81
    0x07, 0x81,                                      // 0x83
    // State 2, offset 0x83
    0x06, 0x00, 0x81,                                // 0x86
    // State 3, offset 0x86
    0x05, 0x81, 0x83, 0x81, 0x81,                    // 0x8b
    // State 4, offset 0x8b
    0x06, 0x83, 0x81,                                // 0x8e
    // State 5, offset 0x8e
    0x05, 0x83, 0x81, 0x81, 0x81,                    // 0x93
    // State 6, offset 0x93
    0x04, 0x81, 0x8b, 0x8b, 0x8b, 0x81, 0x81, 0x81,  // 0x9b
    0x81,                                            // 0x9c
    // State 7, offset 0x9c
    0x06, 0x8b, 0x81,                                // 0x9f
    // State 8, offset 0x9f
    0x04, 0x8b, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,  // 0xa7
    0x81,                                            // 0xa8
};

const size_t kUtf8ValidatorTablesSize = std::size(kUtf8ValidatorTables);

}  // namespace internal
}  // namespace base
