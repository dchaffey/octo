#pragma once

#include <string>

class Cat {
private:
    std::string name; // cat's name
    int age;          // cat's age in years

public:
    Cat(const std::string& catName, int catAge); // constructor

    void meow() const;     // make the cat meow
    std::string getName(); // get cat's name
    int getAge() const;    // get cat's age
};
