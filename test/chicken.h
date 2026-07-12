#ifndef CHICKEN_H
#define CHICKEN_H

#include <string>
#include <cassert>

class Chicken {
private:
  std::string name;        // chicken's identifier
  int age;                 // weeks old, must be non-negative
  int speed_kmh;           // maximum speed in km/h, must be positive
  int energy_level;        // 0-100 scale, determines capability for activity
  int eggs_laid;           // total eggs produced

public:
  // Constructor — initializes chicken with name, age, and max speed; validates inputs
  Chicken(const std::string& n, int a, int s);

  // Returns the chicken's name
  std::string get_name() const;

  // Returns current age in weeks
  int get_age() const;

  // Returns maximum speed capability
  int get_speed() const;

  // Returns current energy level (0-100)
  int get_energy() const;

  // Returns total eggs laid
  int get_eggs_laid() const;

  // Runs/pecks for duration_minutes; consumes energy based on speed
  void run(int duration_minutes);

  // Restores energy; takes minutes to recover
  void rest(int duration_minutes);

  // Consumes energy to eat; recovers some stamina
  void eat();

  // Lays an egg if energy permits; consumes energy
  void lay_egg();

  // Ages the chicken by one week
  void age_one_week();
};

#endif
