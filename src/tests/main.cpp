#include <iostream>
#include "Dog.h"
#include "Cat.h"

int main() {
    std::cout << "hello big new world" << std::endl;

    Dog dog("Rex", 5);
    dog.bark();
    std::cout << "Dog: " << dog.getName() << ", Age: " << dog.getAge() << std::endl;

    Cat cat("Whiskers", 3);
    cat.meow();
    std::cout << "Cat: " << cat.getName() << ", Age: " << cat.getAge() << std::endl;

    return 0;
}
