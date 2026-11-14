// Host unit tests for watchdog_timing.h (hardware watchdog reload maths).
// Build and run: make -C chair test-cpp
#include <cassert>
#include <cstdio>

#include "../../unoq-app/sketch/watchdog_timing.h"

using namespace hwwdt;

namespace {

void test_reload_for_the_sketch_timeout() {
  // 32 kHz LSI / 4 = 8 kHz: 200 ms = 1600 ticks, reload register holds ticks - 1.
  assert(reload_for(200, kLsiHz, kPrescalerDiv) == 1599);
  assert(timeout_ms_for(1599, kLsiHz, kPrescalerDiv) == 200);
}

void test_reload_is_clamped_to_the_register_range() {
  assert(reload_for(10000, kLsiHz, kPrescalerDiv) == kMaxReload);
  assert(reload_for(0, kLsiHz, kPrescalerDiv) == 0);
}

void test_reload_rejects_a_zero_prescaler() {
  assert(reload_for(200, kLsiHz, 0) == 0);
}

// sketch.ino also static_asserts kTimeoutMs <= MAX_TTL_MS.
void test_sketch_timeout_fits_the_register() {
  static_assert(kTimeoutMs > 0, "watchdog timeout must be positive");
  static_assert(reload_for(kTimeoutMs, kLsiHz, kPrescalerDiv) < kMaxReload,
                "timeout must fit the reload register without clamping");
  assert(timeout_ms_for(reload_for(kTimeoutMs, kLsiHz, kPrescalerDiv), kLsiHz, kPrescalerDiv) ==
         kTimeoutMs);
}

}  // namespace

int main() {
  test_reload_for_the_sketch_timeout();
  test_reload_is_clamped_to_the_register_range();
  test_reload_rejects_a_zero_prescaler();
  test_sketch_timeout_fits_the_register();
  std::puts("watchdog_timing: all tests passed");
  return 0;
}
