// SPDX-License-Identifier: MIT
#pragma once

#include <cmath>
#include <cstddef>
#include <limits>

namespace native_asr {

enum class BoundaryError {
    none,
    logical_clock_invalid,
    not_after_previous,
    beyond_delivered,
    beyond_buffered,
};

struct BoundaryResult {
    size_t sample = 0;
    BoundaryError error = BoundaryError::none;
};

inline BoundaryResult resolve_cascade_boundary(
    bool automatic, double logical_threshold_crossing_seconds, size_t previous_boundary,
    size_t delivered_samples, size_t buffered_samples, int sample_rate) {
    if (!automatic) {
        if (delivered_samples > buffered_samples) {
            return {0, BoundaryError::beyond_buffered};
        }
        return {delivered_samples, BoundaryError::none};
    }
    if (!std::isfinite(logical_threshold_crossing_seconds) || sample_rate <= 0) {
        return {0, BoundaryError::logical_clock_invalid};
    }

    const long double scaled =
        static_cast<long double>(logical_threshold_crossing_seconds) * sample_rate;
    constexpr long double kLongLongMaximum =
        static_cast<long double>(std::numeric_limits<long long>::max());
    constexpr long double kLongLongMinimum =
        static_cast<long double>(std::numeric_limits<long long>::min());
    if (!std::isfinite(scaled) || scaled > kLongLongMaximum - 0.5L ||
        scaled < kLongLongMinimum + 0.5L) {
        return {0, BoundaryError::logical_clock_invalid};
    }

    const long long rounded = std::llround(scaled);
    if (rounded <= 0 || static_cast<unsigned long long>(rounded) <= previous_boundary) {
        return {0, BoundaryError::not_after_previous};
    }
    const size_t boundary = static_cast<size_t>(rounded);
    if (boundary > delivered_samples) {
        return {0, BoundaryError::beyond_delivered};
    }
    if (boundary > buffered_samples) {
        return {0, BoundaryError::beyond_buffered};
    }
    return {boundary, BoundaryError::none};
}

inline const char* boundary_error_message(BoundaryError error) {
    switch (error) {
        case BoundaryError::none:
            return "";
        case BoundaryError::logical_clock_invalid:
            return "automatic endpoint has no finite logical threshold-crossing clock";
        case BoundaryError::not_after_previous:
            return "automatic endpoint boundary does not follow the prior source boundary";
        case BoundaryError::beyond_delivered:
            return "automatic endpoint boundary exceeds delivered audio";
        case BoundaryError::beyond_buffered:
            return "endpoint boundary exceeds adapter-buffered audio";
    }
    return "unknown endpoint boundary attribution error";
}

}  // namespace native_asr
