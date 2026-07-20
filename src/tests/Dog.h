#pragma once

#include <string>

class Dog {
private:
    std::string name; // dog's name
    int age;          // dog's age in years

public:
    Dog(const std::string& dogName, int dogAge); // constructor

    void bark() const;      // make the dog bark
    std::string getName(); // get dog's name
    int getAge() const;    // get dog's age
};
