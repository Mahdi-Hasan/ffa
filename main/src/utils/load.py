#!/usr/bin/env python
# -*- coding:utf-8 -*-
__author__ = 'fkb'
__email__ = 'fkb@zjuici.com'


import json


def load_json(path: str):
	with open(path, 'r') as f:
		return json.load(f)