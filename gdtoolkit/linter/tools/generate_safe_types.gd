# Prints the class names that back ENGINE_SAFE_TYPES in
# gdtoolkit/linter/engine_safe_types.py: every registered class that does not
# inherit Node, split into engine classes (the default list) and GDExtension
# classes (a project's `lifetime-safe-types` config).
#
#   godot --headless --script gdtoolkit/linter/tools/generate_safe_types.gd
extends SceneTree


func _init():
	var engine_safe := []
	var extension_safe := []
	for c in ClassDB.get_class_list():
		if ClassDB.is_parent_class(c, "Node"):
			continue
		if ClassDB.class_get_api_type(c) == ClassDB.API_EXTENSION:
			extension_safe.append(c)
		else:
			engine_safe.append(c)
	print("ENGINE_SAFE=", ",".join(engine_safe))
	print("EXTENSION_SAFE=", ",".join(extension_safe))
	quit()
