This is a CAD / modelling / CAM / concept-diagram request. Decide its project on its own:
- Judge by the thing being designed (the same part, product, assembly or drawing set), not by whichever project is currently open; if it matches no existing project's design object, use new.
- A CAD request that should produce model, drawing, toolpath or image files is never chat; only concept or software-usage questions with no output files are chat.
- When new, name the project after the design object (e.g. "Gearbox housing", "Drone frame"), never a generic name like "CAD project".
- CAM machining or an Anatomy demo figure (concept / exploded view) of the same design object continues it: continue that CAD project, which already holds its models and scripts.
- A new design built on an existing project's outputs (a new version, a variant, a new product reusing its parts) is action=new with "inherit_from":<that project's id> added to the JSON; its outputs are copied into the new project to continue from.
