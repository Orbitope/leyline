package shapes

type Square struct{ Side int }

func NewSquare(side int) *Square { return &Square{Side: side} }

func (s *Square) Area() int { return s.Side * s.Side }

// Scale the method hands over to Scale the function: Go has no implicit receiver.
func (s *Square) Scale(f int) *Square { return Scale(s, f) }

func Scale(s *Square, f int) *Square { return NewSquare(s.Side * f) }
