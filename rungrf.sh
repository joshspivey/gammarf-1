#!/bin/bash

IMAGE="fd854a62078b"
sudo docker run -it --net="host" --privileged $IMAGE
