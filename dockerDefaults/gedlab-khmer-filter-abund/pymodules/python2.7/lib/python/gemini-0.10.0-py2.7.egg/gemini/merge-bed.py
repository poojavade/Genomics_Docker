#!/usr/bin/env python

import sys

prev_score = None
start = None
end = None
recs = []
for line in sys.stdin:

	f = line.strip().split('\t')
	score = f[3]

	if score == prev_score:
		chrom = f[0]
		start = f[1]
		while (score == prev_score):
			end = f[1]
			prev_score = score
			line = sys.stdin.next()
			f = line.strip().split('\t')
			score = f[3]
	else:
		print start, end, score
	prev_score = score
	print recs, score, prev_score