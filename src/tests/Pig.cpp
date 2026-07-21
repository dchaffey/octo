#include "Pig.h"
#include <iostream>

Pig::Pig(const std::string& pigName, int pigAge) : name(pigName), age(pigAge) {}

void Pig::oink() const {
    std::cout << name << " says: Oink!" << std::endl;
}

std::string Pig::getName() {
    return name;
}

int Pig::getAge() const {
    return age;
}
