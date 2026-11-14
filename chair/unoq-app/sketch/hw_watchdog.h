// Independent hardware watchdog (STM32 IWDG) for the chair sketch.
//
// The ttl watchdog in chair_control.h runs inside loop(). If loop() itself
// stops (a Bridge handler or the router transport blocks, or a Zephyr fatal
// error halts the CPU), the PWM timer would keep producing the last duty. The
// IWDG runs from its own 32 kHz oscillator and resets the MCU unless refresh()
// feeds it every hwwdt::kTimeoutMs. After a reset every pin is high-impedance
// until setup() centres the axes again.
//
// Direct register access through the STM32 LL driver: the UNO Q's Zephyr
// loader is built without CONFIG_WATCHDOG, so the Zephyr watchdog API has no
// device to talk to. Once started, the IWDG cannot be stopped until reset.
#pragma once

#include <Arduino.h>
#include <stm32u5xx.h>
#include <stm32u5xx_ll_iwdg.h>

#include "watchdog_timing.h"

namespace hwwdt {

// Prescaler register value matching kPrescalerDiv (divide by 4).
const uint32_t kPrescalerBits = LL_IWDG_PRESCALER_4;
// The IWDG needs a few LSI cycles to latch prescaler and reload.
const uint32_t kReadyTimeoutMs = 100;

// Starts the IWDG. Returns false if its registers never became ready; the
// sketch then refuses to drive (fail closed).
inline bool start() {
  // Freeze the counter while a debugger halts the core, so flashing a new
  // sketch over SWD is not cut short by a watchdog reset.
  DBGMCU->APB1FZR1 |= DBGMCU_APB1FZR1_DBG_IWDG_STOP;
  LL_IWDG_Enable(IWDG);
  LL_IWDG_EnableWriteAccess(IWDG);
  LL_IWDG_SetPrescaler(IWDG, kPrescalerBits);
  LL_IWDG_SetReloadCounter(IWDG, reload_for(kTimeoutMs, kLsiHz, kPrescalerDiv));
  const uint32_t started = millis();
  while (!LL_IWDG_IsReady(IWDG)) {
    if (millis() - started > kReadyTimeoutMs) return false;
  }
  LL_IWDG_ReloadCounter(IWDG);
  return true;
}

inline void feed() { LL_IWDG_ReloadCounter(IWDG); }

}  // namespace hwwdt
