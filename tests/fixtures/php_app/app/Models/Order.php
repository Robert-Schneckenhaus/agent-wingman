<?php

namespace App\Models;

use function App\Support\money_round;

/**
 * A customer order.
 */
class Order
{
    use HasStatus;

    public string $status = 'open';

    /** Cancels the order unless it was shipped. */
    public function cancel(): void
    {
        if (!$this->isOpen()) {
            throw new \LogicException('not open');
        }
        $this->status = 'cancelled';
    }

    public function total(array $lines): float
    {
        return money_round(array_sum($lines));
    }
}
