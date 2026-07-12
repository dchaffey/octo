#include "pig.h"

Pig::Pig(const std::string& n, int a, int s)
    : name(n), age(a), speed_kmh(s), energy_level(100), truffles_found(0) {
  assert(!n.empty() && "pig name cannot be empty");
  assert(a >= 0 && "pig age must be non-negative");
  assert(s > 0 && "pig speed must be positive");
}

std::string Pig::get_name() const {
  return name;
}

int Pig::get_age() const {
  return age;
}

int Pig::get_speed() const {
  return speed_kmh;
}

int Pig::get_energy() const {
  return energy_level;
}

int Pig::get_truffles_found() const {
  return truffles_found;
}

void Pig::forage(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_cost = (duration_minutes * speed_kmh) / 40;
  energy_level = (energy_level - energy_cost < 0) ? 0 : energy_level - energy_cost;
}

void Pig::rest(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_gain = (duration_minutes * 2);
  energy_level = (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Pig::eat() {
  int energy_gain = 30;
  energy_level = (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Pig::find_truffle() {
  assert(energy_level >= 25 && "pig must have at least 25 energy to find truffle");

  energy_level -= 25;
  truffles_found++;
}

void Pig::age_one_month() {
  age++;
}
