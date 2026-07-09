#pragma once

#include <string>
#include <utility>

class Dog {
public:
    explicit Dog(std::string name);

    const std::string& name() const;
    std::string bark() const;

private:
    std::string name_;
};

inline Dog::Dog(std::string name) : name_(std::move(name)) {}

inline const std::string& Dog::name() const {
    return name_;
}

inline std::string Dog::bark() const {
    return "Woof! I am " + name_ + ".";
}
