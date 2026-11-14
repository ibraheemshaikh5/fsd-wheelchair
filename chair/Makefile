ifeq ($(origin CXX),default)
CXX := clang++
endif
CXXFLAGS := -std=c++17 -Wall -Wextra -Werror -pedantic -O1
BUILD := build
SKETCH := unoq-app/sketch
CPP_TESTS := $(BUILD)/test_chair_control $(BUILD)/test_watchdog_timing

.PHONY: test test-cpp test-py lint

test: test-cpp test-py

test-cpp: $(CPP_TESTS)
	for t in $(CPP_TESTS); do ./$$t || exit 1; done

$(BUILD)/test_chair_control: tests/cpp/test_chair_control.cpp $(SKETCH)/chair_control.h
	mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ tests/cpp/test_chair_control.cpp

$(BUILD)/test_watchdog_timing: tests/cpp/test_watchdog_timing.cpp $(SKETCH)/watchdog_timing.h
	mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ tests/cpp/test_watchdog_timing.cpp

test-py:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .
