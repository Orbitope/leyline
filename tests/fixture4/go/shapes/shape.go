package shapes

type Square struct{ Side int }

func NewSquare(side int) *Square { return &Square{Side: side} }

func (s *Square) Area() int { return s.Side * s.Side }
