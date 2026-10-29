<?php

namespace App\Models;

trait HasStatus
{
    public function isOpen(): bool
    {
        return $this->status === 'open';
    }
}
