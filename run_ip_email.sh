#!/bin/bash

HOME=/home/pi
VENVDIR=$HOME/venv
BINDIR=$HOME/code/pistreaming

cd $BINDIR
source $VENVDIR/bin/activate
python $BINDIR/ip_startup.py 