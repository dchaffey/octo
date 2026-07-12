#include "horse.h"

Horse::Horse(const std::string& n, int a, int s)
    : name(n), age(a), speed_kmh(s), energy_level(100) {
  assert(!n.empty() && "horse name cannot be empty");
  assert(a >= 0 && "horse age must be non-negative");
  assert(s > 0 && "horse speed must be positive");
}

std::string Horse::get_name() const {
  return name;
}

int Horse::get_age() const {
  return age;
}

int Horse::get_speed() const {
  return speed_kmh;
}

int Horse::get_energy() const {
  return energy_level;
}

void Horse::run(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_cost = (duration_minutes * speed_kmh) / 50;
  energy_level = (energy_level - energy_cost < 0) ? 0 : energy_level - energy_cost;
}

void Horse::rest(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_gain = (duration_minutes * 2);
  energy_level = (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Horse::eat() {
  int energy_gain = 20;
  energy_level = (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Horse::age_one_year() {
  age++;
}
