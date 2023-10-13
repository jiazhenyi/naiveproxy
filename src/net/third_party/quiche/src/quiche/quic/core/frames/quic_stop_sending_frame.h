// Copyright (c) 2018 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef QUICHE_QUIC_CORE_FRAMES_QUIC_STOP_SENDING_FRAME_H_
#define QUICHE_QUIC_CORE_FRAMES_QUIC_STOP_SENDING_FRAME_H_

#include <ostream>

#include "quiche/quic/core/frames/quic_inlined_frame.h"
#include "quiche/quic/core/quic_constants.h"
#include "quiche/quic/core/quic_error_codes.h"
#include "quiche/quic/core/quic_types.h"

namespace quic {

struct QUIC_EXPORT_PRIVATE QuicStopSendingFrame
    : public QuicInlinedFrame<QuicStopSendingFrame> {
  QuicStopSendingFrame();
  QuicStopSendingFrame(QuicControlFrameId control_frame_id,
                       QuicStreamId stream_id,
                       QuicRstStreamErrorCode error_code);
  QuicStopSendingFrame(QuicControlFrameId control_frame_id,
                       QuicStreamId stream_id, QuicResetStreamError error);

  friend QUIC_EXPORT_PRIVATE std::ostream& operator<<(
      std::ostream& os, const QuicStopSendingFrame& frame);

  QuicFrameType type;

  // A unique identifier of this control frame. 0 when this frame is received,
  // and non-zero when sent.
  QuicControlFrameId control_frame_id = kInvalidControlFrameId;
  QuicStreamId stream_id = 0;

  // For an outgoing frame, the error code generated by the application that
  // determines |ietf_error_code| to be sent on the wire; for an incoming frame,
  // the error code inferred from |ietf_error_code| received on the wire.
  QuicRstStreamErrorCode error_code = QUIC_STREAM_NO_ERROR;

  // On-the-wire application error code of the frame.
  uint64_t ietf_error_code = 0;

  // Returns a tuple of both |error_code| and |ietf_error_code|.
  QuicResetStreamError error() const {
    return QuicResetStreamError(error_code, ietf_error_code);
  }
};

}  // namespace quic

#endif  // QUICHE_QUIC_CORE_FRAMES_QUIC_STOP_SENDING_FRAME_H_
