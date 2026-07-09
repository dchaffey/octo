#pragma once

#include <string>
#include <utility>

class Cat {
public:
    explicit Cat(std::string name);

    const std::string& name() const;
    std::string meow() const;

private:
    std::string name_;
};

inline Cat::Cat(std::string name) : name_(std::move(name)) {}

inline const std::string& Cat::name() const {
    return name_;
}

inline std::string Cat::meow() const {
    return "Meow! I am " + name_ + ".";
}

