// SPDX-License-Identifier: MIT

#include <cassert>
#include <cmath>
#include <cstddef>
#include <limits>
#include <utility>
#include <vector>

#include "cascade-boundary.h"

using native_asr::BoundaryError;
using native_asr::resolve_cascade_boundary;

int main() {
    constexpr int rate = 16000;

    // Delivery can be late without changing ownership of the source samples.
    auto result = resolve_cascade_boundary(true, 1.4, 0, 24000, 56000, rate);
    assert(result.error == BoundaryError::none);
    assert(result.sample == 22400);

    // The adapter's positive sample rounding is deterministic, including ties.
    const double tie = 100.5 / rate;
    assert(resolve_cascade_boundary(true, tie, 0, 200, 200, rate).sample == 101);
    assert(resolve_cascade_boundary(
        true, std::nextafter(tie, 0.0), 0, 200, 200, rate).sample == 100);
    assert(resolve_cascade_boundary(
        true, std::nextafter(tie, 1.0), 0, 200, 200, rate).sample == 101);

    assert(resolve_cascade_boundary(
        true, NAN, 0, 200, 200, rate).error == BoundaryError::logical_clock_invalid);
    assert(resolve_cascade_boundary(
        true, INFINITY, 0, 200, 200, rate).error == BoundaryError::logical_clock_invalid);
    assert(resolve_cascade_boundary(
        true, 100.0 / rate, 100, 200, 200, rate).error ==
        BoundaryError::not_after_previous);
    assert(resolve_cascade_boundary(
        true, 201.0 / rate, 0, 200, 400, rate).error ==
        BoundaryError::beyond_delivered);
    assert(resolve_cascade_boundary(
        true, 401.0 / rate, 0, 500, 400, rate).error ==
        BoundaryError::beyond_buffered);

    // EOF owns the delivered frontier and closes a lossless, non-overlapping
    // partition of the complete adapter-owned PCM.
    constexpr size_t audio_samples = 56000;
    std::vector<std::pair<size_t, size_t>> slices;
    size_t previous = 0;
    for (const double crossing : {1.4, 2.75}) {
        result = resolve_cascade_boundary(
            true, crossing, previous, audio_samples, audio_samples, rate);
        assert(result.error == BoundaryError::none);
        slices.emplace_back(previous, result.sample);
        previous = result.sample;
    }
    result = resolve_cascade_boundary(
        false, NAN, previous, audio_samples, audio_samples, rate);
    assert(result.error == BoundaryError::none);
    assert(result.sample == audio_samples);
    slices.emplace_back(previous, result.sample);

    size_t accounted = 0;
    previous = 0;
    for (const auto& [start, end] : slices) {
        assert(start == previous);
        assert(end > start);
        accounted += end - start;
        previous = end;
    }
    assert(previous == audio_samples);
    assert(accounted == audio_samples);
}
