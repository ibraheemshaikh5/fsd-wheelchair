// Timing for the STM32U585 independent watchdog (IWDG).
//
// Pure constexpr maths, no hardware access, so it is unit tested on a Mac
// (chair/tests/cpp/test_watchdog_timing.cpp). hw_watchdog.h does the
// register writes.
#pragma once

#include <stdint.h>

namespace hwwdt {

const uint32_t kLsiHz = 32000;      // IWDG clock: the 32 kHz LSI oscillator.
const uint32_t kPrescalerDiv = 4;   // IWDG_PR = 0 -> divide by 4 (8 kHz tick).
const uint32_t kMaxReload = 0x0FFF; // 12-bit reload register.
const uint32_t kMsPerSecond = 1000;
// Reset the MCU if loop() has not fed the watchdog for this long. Shorter than
// the command ttl cap, so a hung sketch cannot hold an output longer than a
// stale command could.
const uint32_t kTimeoutMs = 200;

// Reload register value for a timeout: the counter runs (reload + 1) ticks.
constexpr uint32_t reload_for(uint32_t timeout_ms, uint32_t lsi_hz, uint32_t div) {
  if (div == 0) return 0;
  const uint64_t ticks = static_cast<uint64_t>(timeout_ms) * lsi_hz / (div * kMsPerSecond);
  if (ticks == 0) return 0;
  return ticks - 1 > kMaxReload ? kMaxReload : static_cast<uint32_t>(ticks - 1);
}

constexpr uint32_t timeout_ms_for(uint32_t reload, uint32_t lsi_hz, uint32_t div) {
  return lsi_hz == 0 ? 0
                     : static_cast<uint32_t>(static_cast<uint64_t>(reload + 1) * div *
                                             kMsPerSecond / lsi_hz);
}

}  // namespace hwwdt
