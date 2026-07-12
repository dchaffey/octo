#include "chicken.h"

Chicken::Chicken(const std::string &n, int a, int s)
    : name(n), age(a), speed_kmh(s), energy_level(100), eggs_laid(0) {
  assert(!n.empty() && "chicken name cannot be empty");
  assert(a >= 0 && "chicken age must be non-negative");
  assert(s > 0 && "chicken speed must be positive");
}

std::string Chicken::get_name() const { return name; }

int Chicken::get_age() const { return age; }

int Chicken::get_speed() const { return speed_kmh; }

int Chicken::get_energy() const { return energy_level; }

int Chicken::get_eggs_laid() const { return eggs_laid; }

void Chicken::run(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_cost = (duration_minutes * speed_kmh) / 30;
  energy_level =
      (energy_level - energy_cost < 0) ? 0 : energy_level - energy_cost;
}

void Chicken::rest(int duration_minutes) {
  assert(duration_minutes > 0 && "duration must be positive");

  int energy_gain = (duration_minutes * 3);
  energy_level =
      (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Chicken::eat() {
  int energy_gain = 25;
  energy_level =
      (energy_level + energy_gain > 100) ? 100 : energy_level + energy_gain;
}

void Chicken::lay_egg() {
  assert(energy_level >= 30 &&
         "chicken must have at least 30 energy to lay egg");

  energy_level -= 30;
  eggs_laid++;
}

void Chicken::age_one_week() { age++; }
