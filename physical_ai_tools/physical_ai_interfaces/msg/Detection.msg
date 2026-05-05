# A single perception detection from the Roboter Studio workflow runtime.
#
# Coordinates are pixel-space in the ORIGINAL camera frame (before any
# letterbox / resize the perception backend may have applied). The
# editor draws bounding boxes by mapping (cx, cy, w, h) into the
# rendered image's coordinate system using its naturalWidth / Height.
#
# This message replaces the geometry_msgs/Point[] + string[] parallel
# arrays in WorkflowStatus.msg up to and including the v1 ship of
# Roboter Studio (commit 8a6314f). Point only has x/y/z and the
# frontend was hacking d.z as the bbox width — see audit §1.6.
int32 cx              # bbox centroid x (px)
int32 cy              # bbox centroid y (px)
int32 w               # bbox width (px)
int32 h               # bbox height (px)
string label          # German label ("rot", "Banane", "tag42", ...)
float32 confidence    # 0..1
