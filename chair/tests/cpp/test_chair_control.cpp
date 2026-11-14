// Host unit tests for chair_control.h (the microcontroller safety logic).
// Build and run: make -C chair test-cpp
#include <cassert>
#include <cmath>
#include <cstdio>

#include "../../unoq-app/sketch/chair_control.h"

using namespace chair;

namespace {

const Limits kLimits = {0.5f, 0.5f, 500};
const Inputs kClear = {false, false};
const Inputs kEstop = {true, false};
const Inputs kJoystick = {false, true};

bool near(float a, float b) { return std::fabs(a - b) < 1e-4f; }

// Boots, steps once with clear inputs and arms with a zero command at t=0.
State armed_at(uint32_t now) {
  State s = step(initial_state(), kClear, kLimits, now);
  s = on_drive(s, 1, 0.0f, 0.0f, 200, now);
  return step(s, kClear, kLimits, now);
}

void test_boots_stopped_with_no_link() {
  const State s = step(initial_state(), kClear, kLimits, 0);
  assert(s.out_v == 0.0f && s.out_w == 0.0f);
  assert(s.reason == Reason::NoLink);
  assert(s.applied_seq == kNoSeq);
}

void test_nonzero_command_before_arming_is_held_at_zero() {
  State s = step(initial_state(), kClear, kLimits, 0);
  s = step(on_drive(s, 1, 0.3f, 0.1f, 200, 0), kClear, kLimits, 10);
  assert(s.out_v == 0.0f && s.out_w == 0.0f);
  assert(s.reason == Reason::NoLink);
}

void test_zero_command_arms_then_motion_follows() {
  State s = armed_at(0);
  assert(s.reason == Reason::None);
  assert(s.applied_seq == 1);
  s = step(on_drive(s, 2, 0.3f, -0.2f, 200, 50), kClear, kLimits, 60);
  assert(near(s.out_v, 0.3f) && near(s.out_w, -0.2f));
  assert(s.reason == Reason::None);
  assert(s.applied_seq == 2);
  assert(s.mode_auto);
}

void test_watchdog_stops_after_ttl() {
  State s = armed_at(0);
  s = on_drive(s, 2, 0.3f, 0.0f, 200, 100);
  assert(near(step(s, kClear, kLimits, 300).out_v, 0.3f));
  s = step(s, kClear, kLimits, 301);
  assert(s.out_v == 0.0f && s.reason == Reason::Watchdog);
  assert(s.applied_seq == kNoSeq);
}

void test_ttl_is_capped_at_max() {
  State s = armed_at(0);
  s = on_drive(s, 2, 0.3f, 0.0f, 5000, 0);
  assert(near(step(s, kClear, kLimits, 500).out_v, 0.3f));
  assert(step(s, kClear, kLimits, 501).reason == Reason::Watchdog);
}

void test_invalid_ttl_and_non_finite_values_are_rejected() {
  const State s = armed_at(0);
  assert(on_drive(s, 2, 0.3f, 0.0f, 0, 0).last_seq == 1);
  assert(on_drive(s, 2, 0.3f, 0.0f, -5, 0).last_seq == 1);
  assert(on_drive(s, 2, NAN, 0.0f, 200, 0).last_seq == 1);
  assert(on_drive(s, 2, 0.1f, INFINITY, 200, 0).last_seq == 1);
}

void test_stale_seq_is_ignored() {
  State s = armed_at(0);
  s = on_drive(s, 5, 0.2f, 0.0f, 200, 0);
  s = on_drive(s, 4, 0.4f, 0.0f, 200, 10);
  s = step(s, kClear, kLimits, 20);
  assert(near(s.out_v, 0.2f) && s.applied_seq == 5);
  s = on_drive(s, 5, 0.4f, 0.0f, 200, 30);
  assert(near(s.cmd_v, 0.2f));
}

void test_speed_and_turn_are_clamped_and_reported_as_limit() {
  State s = armed_at(0);
  s = step(on_drive(s, 2, 2.0f, 3.0f, 200, 0), kClear, kLimits, 1);
  assert(near(s.out_v, 0.5f) && near(s.out_w, 0.5f));
  assert(s.reason == Reason::Limit);
  s = step(on_drive(s, 3, 0.1f, -3.0f, 200, 2), kClear, kLimits, 3);
  assert(near(s.out_w, -0.5f) && s.reason == Reason::Limit);
}

void test_reverse_is_never_output() {
  State s = armed_at(0);
  s = step(on_drive(s, 2, -0.3f, 0.1f, 200, 0), kClear, kLimits, 1);
  assert(s.out_v == 0.0f && near(s.out_w, 0.1f));
  assert(s.reason == Reason::Limit);
}

void test_estop_stops_and_needs_zero_command_to_rearm() {
  State s = armed_at(0);
  s = on_drive(s, 2, 0.3f, 0.0f, 200, 0);
  s = step(s, kEstop, kLimits, 1);
  assert(s.out_v == 0.0f && s.reason == Reason::Estop && s.estop);
  // Released, but the moving command is still fresh: stays stopped.
  s = step(s, kClear, kLimits, 2);
  assert(s.out_v == 0.0f && s.reason == Reason::Estop && !s.estop);
  // A zero command sent while the e-stop is open does not re-arm.
  s = step(on_drive(step(s, kEstop, kLimits, 3), 3, 0.0f, 0.0f, 200, 3),
           kClear, kLimits, 4);
  assert(s.reason == Reason::Estop);
  s = on_drive(s, 4, 0.0f, 0.0f, 200, 5);
  s = step(on_drive(s, 5, 0.3f, 0.0f, 200, 6), kClear, kLimits, 7);
  assert(near(s.out_v, 0.3f) && s.reason == Reason::None);
}

void test_joystick_takes_over_and_reports_manual() {
  State s = armed_at(0);
  s = on_drive(s, 2, 0.3f, 0.0f, 200, 0);
  s = step(s, kJoystick, kLimits, 1);
  assert(s.out_v == 0.0f && s.reason == Reason::Joystick);
  assert(!s.mode_auto && s.joystick);
  s = step(s, kClear, kLimits, 2);
  assert(!s.mode_auto && s.reason == Reason::Joystick && !s.joystick);
  s = step(on_drive(s, 3, 0.0f, 0.0f, 200, 3), kClear, kLimits, 4);
  assert(s.mode_auto && s.reason == Reason::None);
}

void test_estop_wins_over_joystick() {
  const State s = step(armed_at(0), Inputs{true, true}, kLimits, 1);
  assert(s.reason == Reason::Estop);
}

void test_stop_disarms_and_resets_seq() {
  State s = armed_at(0);
  s = on_drive(s, 900, 0.3f, 0.0f, 200, 0);
  s = step(on_stop(s), kClear, kLimits, 1);
  assert(s.out_v == 0.0f && s.reason == Reason::NoLink);
  // A restarted service counts from 0 again; that must be accepted.
  s = step(on_drive(s, 0, 0.0f, 0.0f, 200, 2), kClear, kLimits, 3);
  assert(s.reason == Reason::None && s.applied_seq == 0);
}

void test_millis_wraparound_keeps_watchdog_working() {
  const uint32_t near_wrap = 0xFFFFFF00u;
  State s = armed_at(near_wrap);
  s = on_drive(s, 2, 0.3f, 0.0f, 200, near_wrap);
  assert(near(step(s, kClear, kLimits, near_wrap + 150).out_v, 0.3f));
  assert(step(s, kClear, kLimits, near_wrap + 250).reason == Reason::Watchdog);
}

void test_expired_command_is_forgotten_across_a_full_millis_wrap() {
  const uint32_t t0 = 1000;
  State s = armed_at(t0);
  s = on_drive(s, 2, 0.3f, 0.1f, 200, t0);
  s = step(s, kClear, kLimits, t0 + 1000);
  assert(s.reason == Reason::Watchdog && !s.has_cmd);
  // 2^32 ms later millis() - cmd_ms wraps back into [0, ttl]: still stopped.
  s = step(s, kClear, kLimits, t0 + 50);
  assert(s.out_v == 0.0f && s.out_w == 0.0f);
  assert(s.reason == Reason::Watchdog && s.applied_seq == kNoSeq);
}

void test_fresh_command_after_watchdog_is_followed_but_replays_are_not() {
  State s = armed_at(0);
  s = step(on_drive(s, 2, 0.3f, 0.0f, 200, 0), kClear, kLimits, 1000);
  assert(s.reason == Reason::Watchdog);
  assert(step(on_drive(s, 2, 0.3f, 0.0f, 200, 1001), kClear, kLimits, 1002).out_v == 0.0f);
  s = step(on_drive(s, 3, 0.2f, 0.0f, 200, 1001), kClear, kLimits, 1002);
  assert(near(s.out_v, 0.2f) && s.reason == Reason::None);
}

void test_status_array_encoding() {
  State s = armed_at(0);
  s = step(on_drive(s, 7, 0.25f, -0.125f, 200, 0), kClear, kLimits, 1);
  const StatusArray a = encode_status(s);
  assert(a.v[0] == 1 && a.v[1] == 0 && a.v[2] == 0);
  assert(a.v[3] == 7 && a.v[4] == 250 && a.v[5] == -125);
  assert(a.v[6] == static_cast<int32_t>(Reason::None));
  const StatusArray stopped = encode_status(step(s, kEstop, kLimits, 2));
  assert(stopped.v[2] == 1 && stopped.v[3] == kNoSeq);
  assert(stopped.v[6] == static_cast<int32_t>(Reason::Estop));
}

void test_axis_duty_maps_around_centre() {
  assert(near(axis_duty(0.0f, 0.5f), 0.5f));
  assert(near(axis_duty(0.5f, 0.5f), 0.5f + kAxisSpan));
  assert(near(axis_duty(-0.5f, 0.5f), 0.5f - kAxisSpan));
  assert(near(axis_duty(9.0f, 0.5f), 0.5f + kAxisSpan));
  assert(near(axis_duty(0.1f, 0.0f), 0.5f));
}

void test_pin_levels_fail_safe() {
  // E-stop is normally closed to GND: LOW = OK, HIGH = pressed or wire broken.
  assert(!inputs_from_pins(0, 1).estop_open);
  assert(inputs_from_pins(1, 1).estop_open);
  // Joystick-active pulls LOW while the rider touches the joystick.
  assert(inputs_from_pins(0, 0).joystick_touched);
  assert(!inputs_from_pins(0, 1).joystick_touched);
}

}  // namespace

int main() {
  test_pin_levels_fail_safe();
  test_boots_stopped_with_no_link();
  test_nonzero_command_before_arming_is_held_at_zero();
  test_zero_command_arms_then_motion_follows();
  test_watchdog_stops_after_ttl();
  test_ttl_is_capped_at_max();
  test_invalid_ttl_and_non_finite_values_are_rejected();
  test_stale_seq_is_ignored();
  test_speed_and_turn_are_clamped_and_reported_as_limit();
  test_reverse_is_never_output();
  test_estop_stops_and_needs_zero_command_to_rearm();
  test_joystick_takes_over_and_reports_manual();
  test_estop_wins_over_joystick();
  test_stop_disarms_and_resets_seq();
  test_millis_wraparound_keeps_watchdog_working();
  test_expired_command_is_forgotten_across_a_full_millis_wrap();
  test_fresh_command_after_watchdog_is_followed_but_replays_are_not();
  test_status_array_encoding();
  test_axis_duty_maps_around_centre();
  std::puts("chair_control: all tests passed");
  return 0;
}
