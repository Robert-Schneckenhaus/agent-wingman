<?php

namespace App\Support;

/** Rounds to cents. */
function money_round(float $value): float
{
    return round($value * 100) / 100;
}
