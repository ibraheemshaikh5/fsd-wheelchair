// Chair safety logic for the UNO Q microcontroller.
//
// Pure functions only: a state goes in, a new state comes out. No Arduino or
// Zephyr calls, so the same code is unit tested on a Mac with clang++
// (chair/tests/cpp/test_chair_control.cpp).
//
// Rules enforced here, independent of Linux and the network:
//  - Output is zero unless a command younger than its ttl is held
//    (ttl capped at Limits::max_ttl_ms).
//  - Forward speed is never negative; |v| and |w| are clamped to the caps.
//  - E-stop open (pressed or wire broken) or joystick touched -> stop.
//  - After boot, a Linux "stop", the e-stop or a joystick take-over the chair
//    stays stopped until a zero command (v = 0, w = 0) arrives while both
//    inputs are clear. Only then does a moving command take effect.
#pragma once

#include <float.h>
#include <stdint.h>

namespace chair {

// Reason codes, also sent to Linux in the status array.
enum class Reason : int32_t {
  None = 0,      // Output follows the command exactly.
  Watchdog = 1,  // Command older than its ttl.
  Estop = 2,     // E-stop open, or not yet re-armed after it.
  Joystick = 3,  // Rider took over, or not yet re-armed after it.
  NoLink = 4,    // No command since boot or since Linux said stop.
  Limit = 5,     // Output clamped by a cap or by the no-reverse rule.
};

const int32_t kNoSeq = -1;
const float kAxisCentre = 0.5f;  // Duty at rest (joystick centred).
const float kAxisSpan = 0.25f;   // Duty change at full scale (placeholder).
const int kStatusLength = 7;
const float kMilliPerUnit = 1000.0f;

struct Limits {
  float max_v;          // m/s
  float max_w;          // rad/s
  uint32_t max_ttl_ms;  // Longest ttl the chair will honour.
};

struct Inputs {
  bool estop_open;        // true = pressed or wire broken.
  bool joystick_touched;  // true = rider is using the joystick.
};

struct State {
  bool has_cmd;
  int32_t last_seq;  // kNoSeq when any seq is accepted.
  float cmd_v;
  float cmd_w;
  uint32_t cmd_ms;  // millis() when the command arrived.
  uint32_t ttl_ms;
  bool armed;
  Reason latch;  // Why the chair is disarmed.
  bool estop;
  bool joystick;
  bool mode_auto;
  float out_v;
  float out_w;
  int32_t applied_seq;
  Reason reason;
};

struct StatusArray {
  int32_t v[kStatusLength];
};

inline bool is_finite(float x) { return x == x && x <= FLT_MAX && x >= -FLT_MAX; }

inline float clampf(float x, float lo, float hi) {
  if (x < lo) return lo;
  if (x > hi) return hi;
  return x;
}

inline State initial_state() {
  State s = {};
  s.last_seq = kNoSeq;
  s.latch = Reason::NoLink;
  s.mode_auto = true;
  s.applied_seq = kNoSeq;
  s.reason = Reason::NoLink;
  return s;
}

// Pin levels: 0 = LOW, anything else = HIGH. Both inputs use INPUT_PULLUP.
inline Inputs inputs_from_pins(int estop_level, int joystick_level) {
  Inputs in = {estop_level != 0, joystick_level == 0};
  return in;
}

// A drive command from Linux. Invalid or stale commands leave the state as is.
inline State on_drive(const State& s, int32_t seq, float v, float w, int32_t ttl_ms,
                      uint32_t now_ms) {
  if (!is_finite(v) || !is_finite(w) || ttl_ms <= 0 || seq < 0) return s;
  if (s.last_seq != kNoSeq && seq <= s.last_seq) return s;
  State n = s;
  n.has_cmd = true;
  n.last_seq = seq;
  n.cmd_v = v;
  n.cmd_w = w;
  n.cmd_ms = now_ms;
  n.ttl_ms = static_cast<uint32_t>(ttl_ms);
  if (!s.armed && v == 0.0f && w == 0.0f && !s.estop && !s.joystick) {
    n.armed = true;
  }
  return n;
}

// Linux lost the service (or is starting a new session): stop and disarm.
inline State on_stop(const State& s) {
  State n = s;
  n.has_cmd = false;
  n.last_seq = kNoSeq;
  n.armed = false;
  n.latch = Reason::NoLink;
  return n;
}

inline State stopped(const State& s, Reason why) {
  State n = s;
  n.out_v = 0.0f;
  n.out_w = 0.0f;
  n.applied_seq = kNoSeq;
  n.reason = why;
  return n;
}

inline State follow_command(const State& s, const Limits& lim) {
  State n = s;
  n.out_v = clampf(s.cmd_v, 0.0f, lim.max_v);
  n.out_w = clampf(s.cmd_w, -lim.max_w, lim.max_w);
  n.applied_seq = s.last_seq;
  const bool clamped = n.out_v != s.cmd_v || n.out_w != s.cmd_w;
  n.reason = clamped ? Reason::Limit : Reason::None;
  return n;
}

// Drops an expired command for good. Otherwise, 2^32 ms (~49.7 days) later,
// millis() - cmd_ms would wrap back into [0, ttl] and revive it. last_seq is
// kept, so the same command cannot be replayed either.
inline State forget_command(const State& s) {
  State n = s;
  n.has_cmd = false;
  n.cmd_v = 0.0f;
  n.cmd_w = 0.0f;
  return n;
}

inline bool command_fresh(const State& s, const Limits& lim, uint32_t now_ms) {
  const uint32_t ttl = s.ttl_ms < lim.max_ttl_ms ? s.ttl_ms : lim.max_ttl_ms;
  return s.has_cmd && static_cast<uint32_t>(now_ms - s.cmd_ms) <= ttl;
}

// One control pass: read inputs, decide the output.
inline State step(const State& s, const Inputs& in, const Limits& lim, uint32_t now_ms) {
  State n = s;
  n.estop = in.estop_open;
  n.joystick = in.joystick_touched;
  if (in.estop_open || in.joystick_touched) {
    n.armed = false;
    n.latch = in.estop_open ? Reason::Estop : Reason::Joystick;
  }
  n.mode_auto = !(in.joystick_touched || (!n.armed && n.latch == Reason::Joystick));
  if (!n.armed) return stopped(n, n.latch);
  if (!command_fresh(n, lim, now_ms)) return stopped(forget_command(n), Reason::Watchdog);
  return follow_command(n, lim);
}

inline int32_t to_milli(float x) {
  const float scaled = x * kMilliPerUnit;
  return static_cast<int32_t>(scaled < 0.0f ? scaled - 0.5f : scaled + 0.5f);
}

// [mode_auto, joystick_active, estop, applied_seq, v_mm_s, w_mrad_s, reason]
inline StatusArray encode_status(const State& s) {
  StatusArray a = {{s.mode_auto ? 1 : 0, s.joystick ? 1 : 0, s.estop ? 1 : 0, s.applied_seq,
                    to_milli(s.out_v), to_milli(s.out_w), static_cast<int32_t>(s.reason)}};
  return a;
}

// Duty cycle (0..1) for one joystick-style axis. Placeholder mapping: the real
// WOLF joystick signal levels are unknown until the chair is opened.
inline float axis_duty(float value, float cap) {
  if (!(cap > 0.0f) || !is_finite(value)) return kAxisCentre;
  return kAxisCentre + kAxisSpan * clampf(value / cap, -1.0f, 1.0f);
}

}  // namespace chair
