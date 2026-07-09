#!/usr/bin/env sh
set -eu

c++ -std=c++20 -Wall -Wextra -Wpedantic -O2 main.cpp -o octo_test

