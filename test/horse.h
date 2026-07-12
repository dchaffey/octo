#ifndef HORSE_H
#define HORSE_H

#include <string>
#include <cassert>

class Horse {
private:
  std::string name;        // horse's identifier
  int age;                 // years old, must be non-negative
  int speed_kmh;           // maximum speed in km/h, must be positive
  int energy_level;        // 0-100 scale, determines capability for work

public:
  // Constructor — initializes horse with name, age, and max speed; validates inputs
  Horse(const std::string& n, int a, int s);

  // Returns the horse's name
  std::string get_name() const;

  // Returns current age
  int get_age() const;

  // Returns maximum speed capability
  int get_speed() const;

  // Returns current energy level (0-100)
  int get_energy() const;

  // Gallops for duration_minutes; consumes energy based on speed
  void run(int duration_minutes);

  // Restores energy; takes minutes to recover
  void rest(int duration_minutes);

  // Consumes energy to eat; recovers some stamina
  void eat();

  // Ages the horse by one year
  void age_one_year();
};

#endif
