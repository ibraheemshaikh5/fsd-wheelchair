// Chair link: microcontroller side.
//
// Linux (python/main.py) forwards the driving service's commands over the
// Arduino Router Bridge. Every safety rule lives in chair_control.h and runs
// here, on the STM32, so a crashed Linux side or a dropped network still ends
// in a stopped chair.
//
// PLACEHOLDER OUTPUT: how the WOLF wheelchair's joystick is wired, and what
// signal levels it expects, is unknown until the chair is opened. apply_output()
// drives two 3.3 V PWM pins as joystick-style axes centred at 50 % duty. Do NOT
// connect them to the chair until the real interface has been measured.
#include <Arduino_RouterBridge.h>

#include <array>

#include "chair_control.h"
#include "hw_watchdog.h"

// Hard caps compiled into the sketch. Linux cannot raise them.
const float MAX_SPEED_MPS = 0.5f;
const float MAX_TURN_RADPS = 0.5f;
const uint32_t MAX_TTL_MS = 500;
const chair::Limits LIMITS = {MAX_SPEED_MPS, MAX_TURN_RADPS, MAX_TTL_MS};
static_assert(hwwdt::kTimeoutMs <= MAX_TTL_MS, "hardware watchdog must not outlast the ttl cap");

// Pins (UNO Q header numbering). The STM32's GPIOs are 3.3 V logic: outputs
// swing 0-3.3 V only. Never wire them to the chair without level matching.
const int PIN_SPEED_AXIS = D9;    // PB8, TIM4_CH3, PWM 500 Hz.
const int PIN_TURN_AXIS = D10;    // PB9, TIM4_CH4, PWM 500 Hz.
const int PIN_ESTOP = D2;         // PB3, NC switch to GND: LOW = OK.
const int PIN_JOYSTICK_ACTIVE = D4;  // PA12, LOW = rider touching joystick.
const int PIN_LED_RED = LED4_R;   // RGB LED 4, active LOW.
const int PIN_LED_GREEN = LED4_G;
const int PIN_LED_BLUE = LED4_B;
const int LED_ON = LOW;
const int LED_OFF = HIGH;

const int PWM_BITS = 12;
const int PWM_FULL_SCALE = 1 << PWM_BITS;

using StatusReply = std::array<int32_t, chair::kStatusLength>;
using LimitsReply = std::array<int32_t, 3>;

chair::State state = chair::initial_state();
bool hw_watchdog_running = false;  // Commands are refused until the IWDG runs.

void write_axis(int pin, float duty) {
  analogWrite(pin, static_cast<int>(duty * PWM_FULL_SCALE + 0.5f));
}

void show_state(const chair::State& s) {
  const bool following = s.reason == chair::Reason::None || s.reason == chair::Reason::Limit;
  const bool red = s.reason == chair::Reason::Estop || s.reason == chair::Reason::Joystick;
  const bool green = following || s.reason == chair::Reason::Joystick;
  const bool blue = !following && !red;
  digitalWrite(PIN_LED_RED, red ? LED_ON : LED_OFF);
  digitalWrite(PIN_LED_GREEN, green ? LED_ON : LED_OFF);
  digitalWrite(PIN_LED_BLUE, blue ? LED_ON : LED_OFF);
}

// The ONE place the output leaves the board. Placeholder until the WOLF
// joystick interface is known: v drives the speed axis (forward only), w the
// turn axis (positive w = turn left = lower duty).
void apply_output(float v, float w) {
  write_axis(PIN_SPEED_AXIS, chair::axis_duty(v, MAX_SPEED_MPS));
  write_axis(PIN_TURN_AXIS, chair::axis_duty(-w, MAX_TURN_RADPS));
}

void refresh() {
  const chair::Inputs in =
      chair::inputs_from_pins(digitalRead(PIN_ESTOP), digitalRead(PIN_JOYSTICK_ACTIVE));
  state = chair::step(state, in, LIMITS, millis());
  apply_output(state.out_v, state.out_w);
  show_state(state);
  // Fed only here: if loop() or a Bridge handler hangs, the MCU resets.
  if (hw_watchdog_running) hwwdt::feed();
}

// Bridge handlers. provide_safe runs them inside loop()'s thread, so they never
// race with refresh().
bool rpc_drive(int seq, float v, float w, int ttl_ms) {
  if (!hw_watchdog_running) return false;
  state = chair::on_drive(state, seq, v, w, ttl_ms, millis());
  refresh();
  return state.last_seq == seq;
}

bool rpc_stop() {
  state = chair::on_stop(state);
  refresh();
  return true;
}

// Fixed-size arrays: std::vector would pull in C++ exception support, which
// the Zephyr sketch build does not link.
StatusReply rpc_status() {
  const chair::StatusArray a = chair::encode_status(state);
  StatusReply out = {};
  for (int i = 0; i < chair::kStatusLength; ++i) out[i] = a.v[i];
  return out;
}

// [max_speed_mm_s, max_turn_mrad_s, max_ttl_ms] for the hello message.
LimitsReply rpc_limits() {
  const LimitsReply out = {chair::to_milli(MAX_SPEED_MPS), chair::to_milli(MAX_TURN_RADPS),
                           static_cast<int32_t>(MAX_TTL_MS)};
  return out;
}

void setup() {
  pinMode(PIN_ESTOP, INPUT_PULLUP);
  pinMode(PIN_JOYSTICK_ACTIVE, INPUT_PULLUP);
  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_LED_BLUE, OUTPUT);
  analogWriteResolution(PWM_BITS);
  refresh();  // Outputs centred (stopped) before the Bridge is up.

  Bridge.begin();
  Bridge.provide_safe("drive", rpc_drive);
  Bridge.provide_safe("stop", rpc_stop);
  Bridge.provide_safe("status", rpc_status);
  Bridge.provide_safe("limits", rpc_limits);
  hw_watchdog_running = hwwdt::start();
}

// Never block here: Bridge handlers run between loop() passes, and a pass
// longer than hwwdt::kTimeoutMs resets the MCU (which stops the chair).
void loop() { refresh(); }
