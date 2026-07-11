#ifndef ANIMALS_H
#define ANIMALS_H

#include <string>

class Dog {
private:
    std::string name; // stores the dog's name
    int age; // stores the dog's age in years

public:
    Dog(const std::string& name, int age); // constructor to initialize dog with name and age
    void bark(); // makes the dog bark
    void greet(); // prints dog's introduction
    std::string getName() const; // returns the dog's name
    int getAge() const; // returns the dog's age
};

class Cat {
private:
    std::string name; // stores the cat's name
    int age; // stores the cat's age in years

public:
    Cat(const std::string& name, int age); // constructor to initialize cat with name and age
    void meow(); // makes the cat meow
    void greet(); // prints cat's introduction
    std::string getName() const; // returns the cat's name
    int getAge() const; // returns the cat's age
};

class Horse {
private:
    std::string name; // stores the horse's name
    int age; // stores the horse's age in years

public:
    Horse(const std::string& name, int age); // constructor to initialize horse with name and age
    void neigh(); // makes the horse neigh
    void greet(); // prints horse's introduction
    std::string getName() const; // returns the horse's name
    int getAge() const; // returns the horse's age
};

class Cow {
private:
    std::string name; // stores the cow's name
    int age; // stores the cow's age in years

public:
    Cow(const std::string& name, int age); // constructor to initialize cow with name and age
    void moo(); // makes the cow moo
    void greet(); // prints cow's introduction
    std::string getName() const; // returns the cow's name
    int getAge() const; // returns the cow's age
};

class Chicken {
private:
    std::string name; // stores the chicken's name
    int age; // stores the chicken's age in years

public:
    Chicken(const std::string& name, int age); // constructor to initialize chicken with name and age
    void cluck(); // makes the chicken cluck
    void greet(); // prints chicken's introduction
    std::string getName() const; // returns the chicken's name
    int getAge() const; // returns the chicken's age
};

#endif
