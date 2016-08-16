#!/usr/bin/env python
import os
import sys
from gemini_inheritance_model_utils import GeminiInheritanceModelFactory


def run(parser, args):
    if os.path.exists(args.db):
        mendel_violation_factory = \
            GeminiInheritanceModelFactory(args, model="mendel_violations")
        mendel_violation_factory.get_candidates()

