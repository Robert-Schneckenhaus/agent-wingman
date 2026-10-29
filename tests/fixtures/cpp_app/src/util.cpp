#include "shop/util.h"

namespace shop {
namespace util {

double round(double value) {
    return static_cast<long>(value * 100 + 0.5) / 100.0;
}

}  // namespace util
}  // namespace shop
