#ifndef PIG_H
#define PIG_H

#include <string>
#include <cassert>

class Pig {
private:
  std::string name;        // pig's identifier
  int age;                 // months old, must be non-negative
  int speed_kmh;           // maximum speed in km/h, must be positive
  int energy_level;        // 0-100 scale, determines capability for foraging
  int truffles_found;      // total truffles discovered

public:
  // Constructor — initializes pig with name, age, and max speed; validates inputs
  Pig(const std::string& n, int a, int s);

  // Returns the pig's name
  std::string get_name() const;

  // Returns current age in months
  int get_age() const;

  // Returns maximum speed capability
  int get_speed() const;

  // Returns current energy level (0-100)
  int get_energy() const;

  // Returns total truffles found
  int get_truffles_found() const;

  // Forages/roams for duration_minutes; consumes energy based on speed
  void forage(int duration_minutes);

  // Restores energy; takes minutes to recover
  void rest(int duration_minutes);

  // Consumes energy to eat; recovers some stamina
  void eat();

  // Searches for truffles if energy permits; consumes energy
  void find_truffle();

  // Ages the pig by one month
  void age_one_month();
};

#endif
