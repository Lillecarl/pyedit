package main

func Alpha(x int) int {
	return x
}

type Beta struct {
	gamma int
}

func (b Beta) delta() int {
	return b.gamma
}
