#include "Dog.h"
#include <iostream>

Dog::Dog(const std::string& dogName, int dogAge) : name(dogName), age(dogAge) {}

void Dog::bark() const {
    std::cout << name << " says: Woof!" << std::endl;
}

std::string Dog::getName() {
    return name;
}

int Dog::getAge() const {
    return age;
}
