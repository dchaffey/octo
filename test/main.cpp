#include <iostream>
#include "animals.h"

int main() {
    Dog dog("Buddy", 3);
    Cat cat("Whiskers", 2);
    Horse horse("Thunder", 5);
    Cow cow("Bessie", 4);
    Chicken chicken("Cluckers", 1);

    dog.greet();
    dog.bark();

    std::cout << std::endl;

    cat.greet();
    cat.meow();

    std::cout << std::endl;

    horse.greet();
    horse.neigh();

    std::cout << std::endl;

    cow.greet();
    cow.moo();

    std::cout << std::endl;

    chicken.greet();
    chicken.cluck();

    return 0;
}
