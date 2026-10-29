<?php

namespace App\Http\Controllers;

class ShowOrderController
{
    public function __invoke(int $id)
    {
        return ['id' => $id];
    }
}
