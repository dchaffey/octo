#pragma once

#include <string>

class Pig {
private:
    std::string name; // pig's name
    int age;          // pig's age in years

public:
    Pig(const std::string& pigName, int pigAge); // constructor

    void oink() const;      // make the pig oink
    std::string getName(); // get pig's name
    int getAge() const;    // get pig's age
};
