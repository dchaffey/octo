#include "animals.h"
#include <iostream>

Dog::Dog(const std::string& name, int age) : name(name), age(age) {}

void Dog::bark() {
    std::cout << name << " says: Woof! Woof!" << std::endl;
}

void Dog::greet() {
    std::cout << "Hello, I'm " << name << " the dog, and I'm " << age << " years old." << std::endl;
}

std::string Dog::getName() const {
    return name;
}

int Dog::getAge() const {
    return age;
}

Cat::Cat(const std::string& name, int age) : name(name), age(age) {}

void Cat::meow() {
    std::cout << name << " says: Meow! Meow!" << std::endl;
}

void Cat::greet() {
    std::cout << "Hello, I'm " << name << " the cat, and I'm " << age << " years old." << std::endl;
}

std::string Cat::getName() const {
    return name;
}

int Cat::getAge() const {
    return age;
}

Horse::Horse(const std::string& name, int age) : name(name), age(age) {}

void Horse::neigh() {
    std::cout << name << " says: Neigh! Neigh!" << std::endl;
}

void Horse::greet() {
    std::cout << "Hello, I'm " << name << " the horse, and I'm " << age << " years old." << std::endl;
}

std::string Horse::getName() const {
    return name;
}

int Horse::getAge() const {
    return age;
}

Cow::Cow(const std::string& name, int age) : name(name), age(age) {}

void Cow::moo() {
    std::cout << name << " says: Moo! Moo!" << std::endl;
}

void Cow::greet() {
    std::cout << "Hello, I'm " << name << " the cow, and I'm " << age << " years old." << std::endl;
}

std::string Cow::getName() const {
    return name;
}

int Cow::getAge() const {
    return age;
}

Chicken::Chicken(const std::string& name, int age) : name(name), age(age) {}

void Chicken::cluck() {
    std::cout << name << " says: Cluck! Cluck!" << std::endl;
}

void Chicken::greet() {
    std::cout << "Hello, I'm " << name << " the chicken, and I'm " << age << " years old." << std::endl;
}

std::string Chicken::getName() const {
    return name;
}

int Chicken::getAge() const {
    return age;
}
