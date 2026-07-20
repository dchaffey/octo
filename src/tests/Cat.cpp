#include "Cat.h"
#include <iostream>

Cat::Cat(const std::string& catName, int catAge) : name(catName), age(catAge) {}

void Cat::meow() const {
    std::cout << name << " says: Meow!" << std::endl;
}

std::string Cat::getName() {
    return name;
}

int Cat::getAge() const {
    return age;
}
