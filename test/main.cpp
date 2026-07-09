#include <iostream>
#include "Dog.h"
#include "Cat.h"
#include "Horse.h"

int main() {
    Dog dog("Buddy");
    Cat cat("Mittens");
    std::cout << dog.bark() << '\n';
    std::cout << cat.meow() << '\n';

    Horse horse("Spirit");
    std::cout << horse.neigh() << '\n';

    return 0;
}
