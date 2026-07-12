#ifndef GOAT_H
#define GOAT_H

#include <string>
#include <cassert>

class Goat {
private:
  std::string name;        // goat's identifier
  int age;                 // weeks old, must be non-negative
  int jump_height_cm;      // maximum jump height in cm, must be positive
  int energy_level;        // 0-100 scale, determines capability for activity
  int milk_produced;       // total milk units produced

public:
  // Constructor — initializes goat with name, age, and max jump height; validates inputs
  Goat(const std::string& n, int a, int j);

  // Returns the goat's name
  std::string get_name() const;

  // Returns current age in weeks
  int get_age() const;

  // Returns maximum jump height capability
  int get_jump_height() const;

  // Returns current energy level (0-100)
  int get_energy() const;

  // Returns total milk produced
  int get_milk_produced() const;

  // Jumps and plays for duration_minutes; consumes energy based on jump height
  void jump(int duration_minutes);

  // Restores energy; takes minutes to recover
  void rest(int duration_minutes);

  // Consumes energy to eat; recovers some stamina
  void eat();

  // Produces milk if energy permits; consumes energy
  void produce_milk();

  // Ages the goat by one week
  void age_one_week();
};

#endif
