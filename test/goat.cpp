#include "goat.h"

Goat::Goat(const std::string &n, int a, int j)
    : name(n), age(a), jump_height_cm(j), energy_level(100), milk_produced(0) {
  assert(!n.empty() && "goat name cannot be empty");
  assert(a >= 0 && "goat age must be non-negative");
  assert(j > 0 && "goat jump height must be positive");
}

std::string Goat::get_name() const { return name; }

int Goat::get_age() const { return age; }

int Goat::get_jump_height() const { return jump_height_cm; }

int Goat::get_energy() const { return energy_level; }

int Goat::get_milk_produced() const { return milk_produced; }

void Goat::jump(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_cost = (duration_minutes * jump_height_cm) / 25;
  energy_level =
      (energy_level - energy_cost < 0) ? 0 : energy_level - energy_cost;
}

void Goat::rest(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_gain = (duration_minutes * 3);
  energy_level =
      (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Goat::eat() {
  int energy_gain = 25;
  energy_level =
      (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Goat::produce_milk() {
  assert(energy_level >= 35 &&
         "goat must have at least 35 energy to produce milk");

  energy_level -= 35;
  milk_produced++;
}

void Goat::age_one_week() { age++; }
