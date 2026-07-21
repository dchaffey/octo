#pragma once

#include <string>

class Horse {
private:
    std::string name; // horse's name
    int age;          // horse's age in years

public:
    Horse(const std::string& horseName, int horseAge); // constructor

    void neigh() const;        // make the horse neigh
    std::string getName();     // get horse's name
    int getAge() const;        // get horse's age
};
