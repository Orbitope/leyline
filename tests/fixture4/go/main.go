package main

import (
	"fmt"
	"example.com/fx/shapes"
)

type Options struct {
	Check int
}

func validate(n int) int { return n }

func report(sq *shapes.Square) {
	fmt.Println(sq.Area())
}

func main() {
	sq := shapes.NewSquare(3)
	report(sq)
	opts := Options{Check: validate(1)}
	fmt.Println(opts, sq.Scale(2))
}
