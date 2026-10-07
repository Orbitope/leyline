package main

import (
	"fmt"
	"example.com/fx/shapes"
)

func report(sq *shapes.Square) {
	fmt.Println(sq.Area())
}

func main() {
	sq := shapes.NewSquare(3)
	report(sq)
}
