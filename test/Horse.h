#pragma once

#include <string>
#include <utility>

class Horse {
public:
    explicit Horse(std::string name);

    const std::string& name() const;
    std::string neigh() const;

private:
    std::string name_;
};

inline Horse::Horse(std::string name) : name_(std::move(name)) {}

inline const std::string& Horse::name() const {
    return name_;
}

inline std::string Horse::neigh() const {
    return "Neigh! I am " + name_ + ".";
}

