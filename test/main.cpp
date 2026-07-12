#include <iostream>
#include "horse.h"

int main() {
    Horse stallion("Thunder", 5, 60);

    std::cout << "Horse: " << stallion.get_name() << std::endl;
    std::cout << "Age: " << stallion.get_age() << " years" << std::endl;
    std::cout << "Speed: " << stallion.get_speed() << " km/h" << std::endl;
    std::cout << "Energy: " << stallion.get_energy() << "/100" << std::endl;

    stallion.run(10);
    std::cout << "After 10 min run - Energy: " << stallion.get_energy() << "/100" << std::endl;

    stallion.eat();
    std::cout << "After eating - Energy: " << stallion.get_energy() << "/100" << std::endl;

    return 0;
}
