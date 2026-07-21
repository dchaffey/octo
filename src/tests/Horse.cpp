#include "Horse.h"
#include <iostream>

Horse::Horse(const std::string& horseName, int horseAge) : name(horseName), age(horseAge) {}

void Horse::neigh() const {
    std::cout << name << " says: Neigh!" << std::endl;
}

std::string Horse::getName() {
    return name;
}

int Horse::getAge() const {
    return age;
}
