"""Object-lifetime checks for GDScript running on Godot's release templates.

On a release export the GDScript VM does not validate the receiver of a method
call, an `is`/`as` test or a `for` iteration: if the object was freed, that is
a use-after-free and the process dies with SIGSEGV (the debug template turns
the same access into a logged error). Property get/set is validated in every
build, so only the call-like shapes are dangerous.

Two things make a reference stale without the code looking wrong:

* `await` hands control back to the engine; whatever the function was about
  to touch may be gone when it resumes. The engine protects `self` (a
  coroutine whose instance was freed is never resumed) but nothing else -
  arguments, members, captured locals.
* `if node:` / `node == null` do not detect a freed instance: the reference is
  non-null, so the test passes and the next line crashes.
  `is_instance_valid()` resolves the object id instead of the pointer.

Rules:

* `unguarded-node-access-after-await` - after the nearest preceding `await`
  in a function (or anywhere in a loop body that awaits, or anywhere in a
  lambda), a method call / `is` / `as` on a member, parameter or local that
  may hold a node is only allowed once `is_instance_valid(name)` or
  `NodeGuard.is_alive(name, ...)` was tested, or the name was reassigned.
  Flow-insensitive, in source order.
* `node-null-comparison` - `name == null`, `name != null`, `not name` or a
  bare `if name:` on a name that holds a node.

A name "may hold a node" when its declared type is not matched by the
`lifetime-safe-types` regex (built-ins, resources, RefCounted-like classes)
or, for an untyped name, when it is not matched by `lifetime-safe-names`.
Names starting with an uppercase letter (autoloads, classes, constants) and
`self` are never tracked. `@onready` members live and die with `self`, so
they are exempt from the await rule but not from the null-comparison rule.
"""

import re
from types import MappingProxyType
from typing import Dict, List, Optional, Set, Tuple

from lark import Token, Tree

from ..common.ast import AbstractSyntaxTree, Class, Function
from ..common.utils import get_column, get_line

from .problem import Problem

AWAIT_RULE = "unguarded-node-access-after-await"
ARG_RULE = "unguarded-node-argument-after-await"
NULL_RULE = "node-null-comparison"

GUARD_FUNCTIONS = {"is_instance_valid"}
GUARD_METHODS = {("NodeGuard", "is_alive")}

# Every built-in type and engine class that does not inherit Node, as listed
# by ClassDB in Godot 4.6 (`ClassDB.get_class_list()` filtered with
# `is_parent_class(c, "Node")`). A declared type in this set can never hold a
# freed node. `Object` itself is left out: a variable typed Object can hold a
# node. Regenerate with gdtoolkit/linter/tools/generate_safe_types.gd when the engine version
# changes.
ENGINE_SAFE_TYPES = frozenset(
    """
    AABB AESContext AStar2D AStar3D AStarGrid2D AnimatedTexture Animation
    AnimationLibrary AnimationNode AnimationNodeAdd2 AnimationNodeAdd3
    AnimationNodeAnimation AnimationNodeBlend2 AnimationNodeBlend3
    AnimationNodeBlendSpace1D AnimationNodeBlendSpace2D AnimationNodeBlendTree
    AnimationNodeExtension AnimationNodeOneShot AnimationNodeOutput
    AnimationNodeStateMachine AnimationNodeStateMachinePlayback
    AnimationNodeStateMachineTransition AnimationNodeSub2 AnimationNodeSync
    AnimationNodeTimeScale AnimationNodeTimeSeek AnimationNodeTransition
    AnimationRootNode Array ArrayMesh ArrayOccluder3D AtlasTexture
    AudioBusLayout AudioEffect AudioEffectAmplify AudioEffectBandLimitFilter
    AudioEffectBandPassFilter AudioEffectCapture AudioEffectChorus
    AudioEffectCompressor AudioEffectDelay AudioEffectDistortion AudioEffectEQ
    AudioEffectEQ10 AudioEffectEQ21 AudioEffectEQ6 AudioEffectFilter
    AudioEffectHardLimiter AudioEffectHighPassFilter AudioEffectHighShelfFilter
    AudioEffectInstance AudioEffectLimiter AudioEffectLowPassFilter
    AudioEffectLowShelfFilter AudioEffectNotchFilter AudioEffectPanner
    AudioEffectPhaser AudioEffectPitchShift AudioEffectRecord AudioEffectReverb
    AudioEffectSpectrumAnalyzer AudioEffectSpectrumAnalyzerInstance
    AudioEffectStereoEnhance AudioSample AudioSamplePlayback AudioServer
    AudioStream AudioStreamGenerator AudioStreamGeneratorPlayback
    AudioStreamInteractive AudioStreamMP3 AudioStreamMicrophone
    AudioStreamOggVorbis AudioStreamPlayback AudioStreamPlaybackInteractive
    AudioStreamPlaybackOggVorbis AudioStreamPlaybackPlaylist
    AudioStreamPlaybackPolyphonic AudioStreamPlaybackResampled
    AudioStreamPlaybackSynchronized AudioStreamPlaylist AudioStreamPolyphonic
    AudioStreamRandomizer AudioStreamSynchronized AudioStreamWAV BaseMaterial3D
    Basis BitMap BoneMap BoxMesh BoxOccluder3D BoxShape3D ButtonGroup Callable
    CallbackTweener CameraAttributes CameraAttributesPhysical
    CameraAttributesPractical CameraFeed CameraServer CameraTexture
    CanvasItemMaterial CanvasTexture CapsuleMesh CapsuleShape2D CapsuleShape3D
    CharFXTransform CircleShape2D ClassDB CodeHighlighter Color ColorPalette
    Compositor CompositorEffect CompressedCubemap CompressedCubemapArray
    CompressedTexture2D CompressedTexture2DArray CompressedTexture3D
    CompressedTextureLayered ConcavePolygonShape2D ConcavePolygonShape3D
    ConfigFile ConvexPolygonShape2D ConvexPolygonShape3D Crypto CryptoKey
    Cubemap CubemapArray Curve Curve2D Curve3D CurveTexture CurveXYZTexture
    CylinderMesh CylinderShape3D DPITexture DTLSServer Dictionary DirAccess
    DisplayServer ENetConnection ENetMultiplayerPeer ENetPacketPeer
    EditorContextMenuPlugin EditorDebuggerPlugin EditorDebuggerSession
    EditorExportPlatform EditorExportPlatformAndroid
    EditorExportPlatformAppleEmbedded EditorExportPlatformExtension
    EditorExportPlatformIOS EditorExportPlatformLinuxBSD
    EditorExportPlatformMacOS EditorExportPlatformPC
    EditorExportPlatformVisionOS EditorExportPlatformWeb
    EditorExportPlatformWindows EditorExportPlugin EditorExportPreset
    EditorFeatureProfile EditorFileSystemDirectory
    EditorFileSystemImportFormatSupportQuery EditorImportPlugin
    EditorInspectorPlugin EditorInterface EditorNode3DGizmo
    EditorNode3DGizmoPlugin EditorPaths EditorResourceConversionPlugin
    EditorResourcePreviewGenerator EditorResourceTooltipPlugin
    EditorSceneFormatImporter EditorSceneFormatImporterBlend
    EditorSceneFormatImporterFBX2GLTF EditorSceneFormatImporterGLTF
    EditorSceneFormatImporterUFBX EditorScenePostImport
    EditorScenePostImportPlugin EditorScript EditorSelection EditorSettings
    EditorSyntaxHighlighter EditorTranslationParserPlugin EditorUndoRedoManager
    EditorVCSInterface EncodedObjectAsID Engine EngineDebugger EngineProfiler
    Environment Error Expression ExternalTexture FBXDocument FBXState
    FastNoiseLite FileAccess FogMaterial FoldableGroup Font FontFile
    FontVariation FramebufferCacheRD GDExtension GDExtensionManager GDScript
    GDScriptEditorTranslationParserPlugin GDScriptNativeClass
    GDScriptSyntaxHighlighter GLTFAccessor GLTFAnimation GLTFBufferView
    GLTFCamera GLTFDocument GLTFDocumentExtension
    GLTFDocumentExtensionConvertImporterMesh GLTFDocumentExtensionPhysics
    GLTFDocumentExtensionTextureKTX GLTFDocumentExtensionTextureWebP GLTFLight
    GLTFMesh GLTFNode GLTFObjectModelProperty GLTFPhysicsBody GLTFPhysicsShape
    GLTFSkeleton GLTFSkin GLTFSpecGloss GLTFState GLTFTexture GLTFTextureSampler
    Geometry2D Geometry3D GodotInstance GodotNavigationServer2D
    GodotPhysicsServer2D GodotPhysicsServer3D Gradient GradientTexture1D
    GradientTexture2D HMACContext HTTPClient HashingContext HeightMapShape3D IP
    IPUnix Image ImageFormatLoader ImageFormatLoaderExtension ImageTexture
    ImageTexture3D ImageTextureLayered ImmediateMesh ImporterMesh Input
    InputEvent InputEventAction InputEventFromWindow InputEventGesture
    InputEventJoypadButton InputEventJoypadMotion InputEventKey InputEventMIDI
    InputEventMagnifyGesture InputEventMouse InputEventMouseButton
    InputEventMouseMotion InputEventPanGesture InputEventScreenDrag
    InputEventScreenTouch InputEventShortcut InputEventWithModifiers InputMap
    IntervalTweener JNISingleton JSON JSONRPC JavaClass JavaClassWrapper
    JavaObject JavaScriptBridge JavaScriptObject JointLimitation3D
    JointLimitationCone3D KinematicCollision2D KinematicCollision3D
    LabelSettings LightmapGIData Lightmapper LightmapperRD Logger MainLoop
    Marshalls Material Mesh MeshConvexDecompositionSettings MeshDataTool
    MeshLibrary MeshTexture MethodTweener MissingResource MobileVRInterface
    MovieWriter MovieWriterMJPEG MovieWriterOGV MovieWriterPNGWAV MultiMesh
    MultiplayerAPI MultiplayerAPIExtension MultiplayerPeer
    MultiplayerPeerExtension Mutex NativeMenu NavigationMesh
    NavigationMeshGenerator NavigationMeshSourceGeometryData2D
    NavigationMeshSourceGeometryData3D NavigationPathQueryParameters2D
    NavigationPathQueryParameters3D NavigationPathQueryResult2D
    NavigationPathQueryResult3D NavigationPolygon NavigationServer2D
    NavigationServer2DManager NavigationServer3D NavigationServer3DManager
    Node3DGizmo NodePath Noise NoiseTexture2D NoiseTexture3D ORMMaterial3D OS
    Occluder3D OccluderPolygon2D OfflineMultiplayerPeer OggPacketSequence
    OggPacketSequencePlayback OpenXRAPIExtension OpenXRAction
    OpenXRActionBindingModifier OpenXRActionMap OpenXRActionSet
    OpenXRAnalogThresholdModifier OpenXRAnchorTracker
    OpenXRAndroidThreadSettingsExtension OpenXRBindingModifier
    OpenXRDpadBindingModifier OpenXRExtensionWrapper
    OpenXRExtensionWrapperExtension OpenXRFrameSynthesisExtension
    OpenXRFutureExtension OpenXRFutureResult OpenXRHapticBase
    OpenXRHapticVibration OpenXRIPBinding OpenXRIPBindingModifier
    OpenXRInteractionProfile OpenXRInteractionProfileMetadata OpenXRInterface
    OpenXRMarkerTracker OpenXRPlaneTracker OpenXRRenderModelExtension
    OpenXRSpatialAnchorCapability OpenXRSpatialCapabilityConfigurationAnchor
    OpenXRSpatialCapabilityConfigurationAprilTag
    OpenXRSpatialCapabilityConfigurationAruco
    OpenXRSpatialCapabilityConfigurationBaseHeader
    OpenXRSpatialCapabilityConfigurationMicroQrCode
    OpenXRSpatialCapabilityConfigurationPlaneTracking
    OpenXRSpatialCapabilityConfigurationQrCode OpenXRSpatialComponentAnchorList
    OpenXRSpatialComponentBounded2DList OpenXRSpatialComponentBounded3DList
    OpenXRSpatialComponentData OpenXRSpatialComponentMarkerList
    OpenXRSpatialComponentMesh2DList OpenXRSpatialComponentMesh3DList
    OpenXRSpatialComponentParentList OpenXRSpatialComponentPersistenceList
    OpenXRSpatialComponentPlaneAlignmentList
    OpenXRSpatialComponentPlaneSemanticLabelList
    OpenXRSpatialComponentPolygon2DList OpenXRSpatialContextPersistenceConfig
    OpenXRSpatialEntityExtension OpenXRSpatialEntityTracker
    OpenXRSpatialMarkerTrackingCapability OpenXRSpatialPlaneTrackingCapability
    OpenXRSpatialQueryResultData OpenXRStructureBase OptimizedTranslation
    PCKPacker PackedByteArray PackedColorArray PackedDataContainer
    PackedDataContainerRef PackedFloat32Array PackedFloat64Array
    PackedInt32Array PackedInt64Array PackedScene PackedStringArray
    PackedVector2Array PackedVector3Array PackedVector4Array PacketPeer
    PacketPeerDTLS PacketPeerExtension PacketPeerStream PacketPeerUDP
    PanoramaSkyMaterial ParticleProcessMaterial Performance PhysicalSkyMaterial
    PhysicsDirectBodyState2D PhysicsDirectBodyState2DExtension
    PhysicsDirectBodyState3D PhysicsDirectBodyState3DExtension
    PhysicsDirectSpaceState2D PhysicsDirectSpaceState2DExtension
    PhysicsDirectSpaceState3D PhysicsDirectSpaceState3DExtension PhysicsMaterial
    PhysicsPointQueryParameters2D PhysicsPointQueryParameters3D
    PhysicsRayQueryParameters2D PhysicsRayQueryParameters3D PhysicsServer2D
    PhysicsServer2DExtension PhysicsServer2DManager PhysicsServer3D
    PhysicsServer3DExtension PhysicsServer3DManager
    PhysicsServer3DRenderingServerHandler PhysicsShapeQueryParameters2D
    PhysicsShapeQueryParameters3D PhysicsTestMotionParameters2D
    PhysicsTestMotionParameters3D PhysicsTestMotionResult2D
    PhysicsTestMotionResult3D PlaceholderCubemap PlaceholderCubemapArray
    PlaceholderMaterial PlaceholderMesh PlaceholderTexture2D
    PlaceholderTexture2DArray PlaceholderTexture3D PlaceholderTextureLayered
    Plane PlaneMesh PointMesh PolygonOccluder3D PolygonPathFinder
    PortableCompressedTexture2D PrimitiveMesh PrismMesh ProceduralSkyMaterial
    ProjectSettings Projection PropertyTweener QuadMesh QuadOccluder3D
    Quaternion RDAttachmentFormat RDFramebufferPass RDPipelineColorBlendState
    RDPipelineColorBlendStateAttachment RDPipelineDepthStencilState
    RDPipelineMultisampleState RDPipelineRasterizationState
    RDPipelineSpecializationConstant RDSamplerState RDShaderFile RDShaderSPIRV
    RDShaderSource RDTextureFormat RDTextureView RDUniform RDVertexAttribute RID
    RandomNumberGenerator Rect2 Rect2i RectangleShape2D RefCounted RegEx
    RegExMatch RenderData RenderDataExtension RenderDataRD RenderSceneBuffers
    RenderSceneBuffersConfiguration RenderSceneBuffersExtension
    RenderSceneBuffersRD RenderSceneData RenderSceneDataExtension
    RenderSceneDataRD RenderingDevice RenderingServer Resource
    ResourceFormatImporterSaver ResourceFormatLoader ResourceFormatSaver
    ResourceImporter ResourceImporterBMFont ResourceImporterBitMap
    ResourceImporterCSVTranslation ResourceImporterDynamicFont
    ResourceImporterImage ResourceImporterImageFont
    ResourceImporterLayeredTexture ResourceImporterMP3 ResourceImporterOBJ
    ResourceImporterOggVorbis ResourceImporterSVG ResourceImporterScene
    ResourceImporterShaderFile ResourceImporterTexture
    ResourceImporterTextureAtlas ResourceImporterWAV ResourceLoader
    ResourceSaver ResourceUID RibbonTrailMesh RichTextEffect SceneCacheInterface
    SceneMultiplayer SceneRPCInterface SceneReplicationConfig
    SceneReplicationInterface SceneState SceneTree SceneTreeTimer Script
    ScriptBacktrace ScriptExtension ScriptLanguage ScriptLanguageExtension
    SegmentShape2D Semaphore SentryEditorExportPluginAndroid
    SentryEditorExportPluginIOS SentryEditorExportPluginUnix
    SentryEditorExportPluginWeb SeparationRayShape2D SeparationRayShape3D Shader
    ShaderInclude ShaderIncludeDB ShaderMaterial Shape2D Shape3D Shortcut Signal
    SkeletonModification2D SkeletonModification2DCCDIK
    SkeletonModification2DFABRIK SkeletonModification2DJiggle
    SkeletonModification2DLookAt SkeletonModification2DPhysicalBones
    SkeletonModification2DStackHolder SkeletonModification2DTwoBoneIK
    SkeletonModificationStack2D SkeletonProfile SkeletonProfileHumanoid Skin
    SkinReference Sky SocketServer SphereMesh SphereOccluder3D SphereShape3D
    SpriteFrames StandardMaterial3D StreamPeer StreamPeerBuffer
    StreamPeerExtension StreamPeerGZIP StreamPeerSocket StreamPeerTCP
    StreamPeerTLS StreamPeerUDS String StringName StyleBox StyleBoxEmpty
    StyleBoxFlat StyleBoxLine StyleBoxTexture SubtweenTweener SurfaceTool
    SyntaxHighlighter SystemFont TCPServer TLSOptions TextLine TextMesh
    TextParagraph TextServer TextServerAdvanced TextServerDummy
    TextServerExtension TextServerFallback TextServerManager Texture Texture2D
    Texture2DArray Texture2DArrayRD Texture2DRD Texture3D Texture3DRD
    TextureCubemapArrayRD TextureCubemapRD TextureLayered TextureLayeredRD Theme
    ThemeContext ThemeDB Thread TileData TileMapPattern TileSet
    TileSetAtlasSource TileSetScenesCollectionSource TileSetSource Time
    TorusMesh Transform2D Transform3D Translation TranslationDomain
    TranslationServer TreeItem TriangleMesh TubeTrailMesh Tween Tweener
    UDPServer UDSServer UPNP UPNPDevice UndoRedo UniformSetCacheRD Variant
    Vector2 Vector2i Vector3 Vector3i Vector4 Vector4i VideoStream
    VideoStreamPlayback VideoStreamTheora ViewportTexture VisualShader
    VisualShaderNode VisualShaderNodeBillboard VisualShaderNodeBooleanConstant
    VisualShaderNodeBooleanParameter VisualShaderNodeClamp
    VisualShaderNodeColorConstant VisualShaderNodeColorFunc
    VisualShaderNodeColorOp VisualShaderNodeColorParameter
    VisualShaderNodeComment VisualShaderNodeCompare VisualShaderNodeConstant
    VisualShaderNodeCubemap VisualShaderNodeCubemapParameter
    VisualShaderNodeCurveTexture VisualShaderNodeCurveXYZTexture
    VisualShaderNodeCustom VisualShaderNodeDerivativeFunc
    VisualShaderNodeDeterminant VisualShaderNodeDistanceFade
    VisualShaderNodeDotProduct VisualShaderNodeExpression
    VisualShaderNodeFaceForward VisualShaderNodeFloatConstant
    VisualShaderNodeFloatFunc VisualShaderNodeFloatOp
    VisualShaderNodeFloatParameter VisualShaderNodeFrame VisualShaderNodeFresnel
    VisualShaderNodeGlobalExpression VisualShaderNodeGroupBase
    VisualShaderNodeIf VisualShaderNodeInput VisualShaderNodeIntConstant
    VisualShaderNodeIntFunc VisualShaderNodeIntOp VisualShaderNodeIntParameter
    VisualShaderNodeIs VisualShaderNodeLinearSceneDepth VisualShaderNodeMix
    VisualShaderNodeMultiplyAdd VisualShaderNodeOuterProduct
    VisualShaderNodeOutput VisualShaderNodeParameter
    VisualShaderNodeParameterRef VisualShaderNodeParticleAccelerator
    VisualShaderNodeParticleBoxEmitter VisualShaderNodeParticleConeVelocity
    VisualShaderNodeParticleEmit VisualShaderNodeParticleEmitter
    VisualShaderNodeParticleMeshEmitter
    VisualShaderNodeParticleMultiplyByAxisAngle VisualShaderNodeParticleOutput
    VisualShaderNodeParticleRandomness VisualShaderNodeParticleRingEmitter
    VisualShaderNodeParticleSphereEmitter VisualShaderNodeProximityFade
    VisualShaderNodeRandomRange VisualShaderNodeRemap VisualShaderNodeReroute
    VisualShaderNodeResizableBase VisualShaderNodeRotationByAxis
    VisualShaderNodeSDFRaymarch VisualShaderNodeSDFToScreenUV
    VisualShaderNodeSample3D VisualShaderNodeScreenNormalWorldSpace
    VisualShaderNodeScreenUVToSDF VisualShaderNodeSmoothStep
    VisualShaderNodeStep VisualShaderNodeSwitch VisualShaderNodeTexture
    VisualShaderNodeTexture2DArray VisualShaderNodeTexture2DArrayParameter
    VisualShaderNodeTexture2DParameter VisualShaderNodeTexture3D
    VisualShaderNodeTexture3DParameter VisualShaderNodeTextureParameter
    VisualShaderNodeTextureParameterTriplanar VisualShaderNodeTextureSDF
    VisualShaderNodeTextureSDFNormal VisualShaderNodeTransformCompose
    VisualShaderNodeTransformConstant VisualShaderNodeTransformDecompose
    VisualShaderNodeTransformFunc VisualShaderNodeTransformOp
    VisualShaderNodeTransformParameter VisualShaderNodeTransformVecMult
    VisualShaderNodeUIntConstant VisualShaderNodeUIntFunc VisualShaderNodeUIntOp
    VisualShaderNodeUIntParameter VisualShaderNodeUVFunc
    VisualShaderNodeUVPolarCoord VisualShaderNodeVarying
    VisualShaderNodeVaryingGetter VisualShaderNodeVaryingSetter
    VisualShaderNodeVec2Constant VisualShaderNodeVec2Parameter
    VisualShaderNodeVec3Constant VisualShaderNodeVec3Parameter
    VisualShaderNodeVec4Constant VisualShaderNodeVec4Parameter
    VisualShaderNodeVectorBase VisualShaderNodeVectorCompose
    VisualShaderNodeVectorDecompose VisualShaderNodeVectorDistance
    VisualShaderNodeVectorFunc VisualShaderNodeVectorLen
    VisualShaderNodeVectorOp VisualShaderNodeVectorRefract
    VisualShaderNodeWorldPositionFromDepth VoxelGIData WeakRef WebRTCDataChannel
    WebRTCDataChannelExtension WebRTCMultiplayerPeer WebRTCPeerConnection
    WebRTCPeerConnectionExtension WebSocketMultiplayerPeer WebSocketPeer
    WebXRInterface WorkerThreadPool World2D World3D WorldBoundaryShape2D
    WorldBoundaryShape3D X509Certificate XMLParser XRBodyTracker
    XRControllerTracker XRFaceTracker XRHandTracker XRInterface
    XRInterfaceExtension XRPose XRPositionalTracker XRServer XRTracker XRVRS
    ZIPPacker ZIPReader bool float int void
    """.split()
)

Position = Tuple[int, int]


# pylint: disable-next=too-few-public-methods
class _TrackedName:
    """One member / parameter / local visible inside a function body."""

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        name: str,
        declared_at: Optional[Position],
        node_like: bool,
        scene_owned: bool,
        typed_node: bool,
    ):
        self.name = name
        self.declared_at = declared_at
        # May hold a node: declared type not known-safe, or untyped with a
        # name that does not look like plain data.
        self.node_like = node_like
        # Looked up from the scene (@onready / $ / % / get_node): lives and
        # dies with self, so an await cannot leave it dangling.
        self.scene_owned = scene_owned
        # Declared with a type that is not known-safe (or scene-owned): the
        # null-comparison rule only fires on these, untyped names are too noisy.
        self.typed_node = typed_node
        # Position of the last guard or (re)assignment seen in source order.
        self.cleared_at = None  # type: Optional[Position]


class _Scope:
    """Names visible in one function (or lambda) body."""

    def __init__(self, parent: Optional["_Scope"] = None):
        self.parent = parent
        self.names = {}  # type: Dict[str, _TrackedName]

    def declare(self, tracked: _TrackedName) -> None:
        self.names[tracked.name] = tracked

    def lookup(self, name: str) -> Optional[_TrackedName]:
        scope = self  # type: Optional[_Scope]
        while scope is not None:
            if name in scope.names:
                return scope.names[name]
            scope = scope.parent
        return None


def lint(parse_tree: Tree, config: MappingProxyType) -> List[Problem]:
    disable = config["disable"]
    run_await = AWAIT_RULE not in disable
    run_arg = ARG_RULE not in disable
    run_null = NULL_RULE not in disable
    if not run_await and not run_arg and not run_null:
        return []
    safe_types = re.compile(config["lifetime-safe-types"])
    safe_names = re.compile(config["lifetime-safe-names"])
    ast = AbstractSyntaxTree(parse_tree)
    problems = []  # type: List[Problem]
    for a_class in ast.all_classes:
        members = _collect_members(a_class, safe_types, safe_names)
        # The AST helper skips `static func`; its node wraps a plain func_def.
        functions = list(a_class.functions) + [
            Function(s.lark_node.children[0])
            for s in a_class.statements
            if s.kind == "static_func_def"
        ]
        for function in functions:
            checker = _FunctionChecker(
                function,
                members,
                safe_types,
                safe_names,
                (run_await, run_arg, run_null),
            )
            problems += checker.run()
    return problems


# --- declarations ---------------------------------------------------------


def _collect_members(
    a_class: Class, safe_types: "re.Pattern", safe_names: "re.Pattern"
) -> Dict[str, _TrackedName]:
    members = {}  # type: Dict[str, _TrackedName]
    own_children = _members_added_as_children(a_class)
    for statement in a_class.statements:
        if statement.kind not in ("class_var_stmt", "static_class_var_stmt"):
            continue
        var_node = statement.lark_node.children[0]
        if var_node.data == "class_var_stmt":  # static var: one level deeper
            var_node = var_node.children[0]
        name, type_hint, initializer = _split_var_node(var_node)
        if name is None:
            continue
        onready = any(a.name == "onready" for a in statement.annotations)
        scene_owned = onready or _is_node_lookup(initializer) or name in own_children
        node_like = scene_owned or _is_node_like(
            name, type_hint, safe_types, safe_names
        )
        typed_node = scene_owned or _is_typed_node(type_hint, safe_types)
        members[name] = _TrackedName(name, None, node_like, scene_owned, typed_node)
    return members


def _members_added_as_children(a_class: Class) -> Set[str]:
    """Names passed to a bare `add_child(name)` anywhere in the class: a node
    the instance parents itself is freed with it, like an @onready one."""
    names = set()  # type: Set[str]
    for node in a_class.lark_node.iter_subtrees():
        if node.data != "standalone_call":
            continue
        callee = node.children[0]
        if (
            isinstance(callee, Token)
            and callee.value == "add_child"
            and len(node.children) > 1
        ):
            child = _single_name(node.children[1])
            if child is not None:
                names.add(child)
    return names


def _split_var_node(var_node: Tree):
    """NAME, TYPE_HINT (or None) and initializer expr (or None) of a var node.

    An untyped `var x = Foo.new()` / `var x := Foo.new()` counts as typed Foo.
    """
    name = None
    type_hint = None
    initializer = None
    for child in var_node.children:
        if isinstance(child, Token):
            if child.type == "NAME" and name is None:
                name = child.value
            elif child.type == "TYPE_HINT":
                type_hint = child.value
        elif isinstance(child, Tree) and child.data == "expr":
            initializer = child
    if type_hint is None:
        type_hint = _constructed_type(initializer)
    return name, type_hint, initializer


def _constructed_type(expr: Optional[Tree]) -> Optional[str]:
    """`Foo.new(...)` -> "Foo"; anything else -> None."""
    if expr is None or len(expr.children) != 1:
        return None
    value = expr.children[0]
    if not isinstance(value, Tree) or value.data != "getattr_call":
        return None
    getattr_node = value.children[0]
    parts = [
        c.value
        for c in getattr_node.children
        if isinstance(c, Token) and c.type == "NAME"
    ]
    if len(parts) == 2 and parts[1] == "new" and parts[0][0].isupper():
        return parts[0]
    return None


def _is_node_lookup(expr: Optional[Tree]) -> bool:
    """`$Path`, `%Unique` or get_node*(...) anywhere in an initializer."""
    if expr is None:
        return False
    for node in expr.iter_subtrees():
        if node.data in ("get_node", "unique_node_path"):
            return True
        if node.data == "standalone_call":
            callee = node.children[0]
            if isinstance(callee, Token) and callee.value.startswith("get_node"):
                return True
        if node.data == "getattr_call":
            method = _last_attr(node.children[0])
            if method is not None and method.startswith("get_node"):
                return True
    return False


def _is_node_like(
    name: str,
    type_hint: Optional[str],
    safe_types: "re.Pattern",
    safe_names: "re.Pattern",
) -> bool:
    if type_hint is not None:
        return _is_typed_node(type_hint, safe_types)
    return safe_names.match(name) is None


def _is_typed_node(type_hint: Optional[str], safe_types: "re.Pattern") -> bool:
    """A declared type that is neither built-in, engine non-Node, nor listed
    as safe by the project. `Array[Foo]` is an Array; `Outer.Inner` (enums,
    inner classes) is treated as safe."""
    if type_hint is None:
        return False
    base = type_hint.split("[", 1)[0].strip()
    if "." in base:
        return False
    if base in ENGINE_SAFE_TYPES:
        return False
    return safe_types.match(base) is None


def _is_safe_initializer(expr: Optional[Tree], safe_types: "re.Pattern") -> bool:
    """Literal, collection or `SafeType.new()` initializers cannot hold a node."""
    if expr is None:
        return False
    if len(expr.children) != 1:
        return False
    value = expr.children[0]
    if isinstance(value, Token):
        return value.type != "NAME" or value.value in ("null", "true", "false")
    if value.data in ("string", "rstring", "array", "dict", "string_name", "node_path"):
        return True
    if value.data == "getattr_call":
        getattr_node = value.children[0]
        parts = [
            c
            for c in getattr_node.children
            if isinstance(c, Token) and c.type == "NAME"
        ]
        if len(parts) == 2 and parts[1].value == "new":
            return safe_types.match(parts[0].value) is not None
    return False


def _last_attr(getattr_node: Tree) -> Optional[str]:
    names = [
        c.value
        for c in getattr_node.children
        if isinstance(c, Token) and c.type in ("NAME", "GET", "SET")
    ]
    return names[-1] if names else None


def _receiver_name(getattr_node: Tree) -> Tuple[Optional[str], bool]:
    """Root name of the receiver of `root[.prop...].method(...)`.

    Returns (name, direct): direct is True for `name.method()` and
    `self.name.method()`, False when properties are read in between
    (`name.child.method()`) - the root may be alive while the property holds
    a freed node, so both shapes are reported. Calls in the chain
    (`name.get_x().method()`) are reported through the inner call instead.
    """
    children = getattr_node.children
    if not isinstance(children[0], Token) or children[0].type != "NAME":
        return None, False
    names = [
        c.value
        for c in children
        if isinstance(c, Token) and c.type in ("NAME", "GET", "SET")
    ]
    if names[0] == "self":
        names = names[1:]
        if not names:
            return None, False
    if len(names) == 1:
        return None, False
    return names[0], len(names) == 2


def _single_name(node) -> Optional[str]:
    """A bare NAME token, or `self.NAME`, as a name string."""
    if isinstance(node, Tree) and node.data == "expr" and len(node.children) == 1:
        return _single_name(node.children[0])
    if isinstance(node, Token):
        return node.value if node.type == "NAME" else None
    if isinstance(node, Tree) and node.data == "getattr":
        children = node.children
        if (
            len(children) == 3
            and isinstance(children[0], Token)
            and children[0].value == "self"
            and isinstance(children[2], Token)
        ):
            return children[2].value
    return None


def _position(node) -> Position:
    return (get_line(node), get_column(node))


def _end_position(node) -> Position:
    if isinstance(node, Tree):
        return (node.meta.end_line, node.meta.end_column)
    return (node.end_line, node.end_column)


def _is_trackable(name: str) -> bool:
    return name != "self" and not name[0].isupper()


# --- per-function walk ----------------------------------------------------


# pylint: disable-next=too-few-public-methods
class _FunctionChecker:
    # pylint: disable=too-many-instance-attributes,too-many-arguments
    # pylint: disable=too-many-positional-arguments
    def __init__(
        self,
        function: Function,
        members: Dict[str, _TrackedName],
        safe_types: "re.Pattern",
        safe_names: "re.Pattern",
        enabled: Tuple[bool, bool, bool],
    ):
        self.function = function
        self.safe_types = safe_types
        self.safe_names = safe_names
        self.run_await, self.run_arg, self.run_null = enabled
        self.problems = []  # type: List[Problem]
        self.reported = set()  # type: Set[Tuple[str, Position]]
        self.nearest_await = None  # type: Optional[Position]
        self.scope = _Scope()
        for member in members.values():
            self.scope.declare(
                _TrackedName(
                    member.name,
                    None,
                    member.node_like,
                    member.scene_owned,
                    member.typed_node,
                )
            )
        self._declare_parameters(function.lark_node.children[0], self.scope)

    def run(self) -> List[Problem]:
        for statement in self.function.lark_node.children[1:]:
            self._walk_statement(statement)
        return self.problems

    # -- declarations

    def _declare_parameters(self, header: Tree, scope: _Scope) -> None:
        for node in header.iter_subtrees():
            if not node.data.startswith("func_arg_"):
                continue
            name = None
            type_hint = None
            for child in node.children:
                if isinstance(child, Token) and child.type == "NAME" and name is None:
                    name = child.value
                elif isinstance(child, Token) and child.type == "TYPE_HINT":
                    type_hint = child.value
            if name is None:
                continue
            node_like = _is_node_like(name, type_hint, self.safe_types, self.safe_names)
            typed_node = _is_typed_node(type_hint, self.safe_types)
            scope.declare(
                _TrackedName(name, _position(node), node_like, False, typed_node)
            )

    def _declare_local(self, var_node: Tree) -> None:
        name, type_hint, initializer = _split_var_node(var_node)
        if name is None:
            return
        if initializer is not None:
            self._walk_expr(initializer)
        node_like = _is_node_like(name, type_hint, self.safe_types, self.safe_names)
        if type_hint is None and _is_safe_initializer(initializer, self.safe_types):
            node_like = False
        typed_node = _is_typed_node(type_hint, self.safe_types)
        tracked = _TrackedName(name, _position(var_node), node_like, False, typed_node)
        # A declaration is an assignment: fresh until the next await. Dated at
        # the end of the statement so `var x = await f()` counts as after it.
        tracked.cleared_at = _end_position(var_node)
        self.scope.declare(tracked)

    # -- statements

    def _walk_statements(self, nodes) -> None:
        for node in nodes:
            if isinstance(node, Tree):
                self._walk_statement(node)

    # pylint: disable-next=too-many-branches
    def _walk_statement(self, node: Tree) -> None:
        kind = node.data
        if kind in ("expr_stmt", "return_stmt"):
            for child in node.children:
                self._walk_expr(child)
        elif kind == "func_var_stmt":
            self._declare_local(node.children[0])
        elif kind == "const_stmt":
            pass
        elif kind == "if_stmt":
            for branch in node.children:
                if branch.data in ("if_branch", "elif_branch"):
                    self._check_condition(branch.children[0])
                    self._walk_expr(branch.children[0])
                    self._walk_statements(branch.children[1:])
                else:
                    self._walk_statements(branch.children)
        elif kind == "while_stmt":
            self._enter_loop(node)
            self._check_condition(node.children[0])
            self._walk_expr(node.children[0])
            self._walk_statements(node.children[1:])
        elif kind in ("for_stmt", "for_stmt_typed"):
            self._enter_loop(node)
            offset = 1 if kind == "for_stmt" else 2
            self._walk_expr(node.children[offset])
            loop_var = node.children[0]
            type_hint = node.children[1].value if kind == "for_stmt_typed" else None
            node_like = _is_node_like(
                loop_var.value, type_hint, self.safe_types, self.safe_names
            )
            typed_node = _is_typed_node(type_hint, self.safe_types)
            tracked = _TrackedName(
                loop_var.value, _position(node), node_like, False, typed_node
            )
            # Reassigned at the top of every iteration: fresh until an await
            # inside the body (the loop-start await point above is dated
            # before this, so a body that only awaits after using it is fine).
            line, column = _position(node)
            tracked.cleared_at = (line, column + 1)
            self.scope.declare(tracked)
            self._walk_statements(node.children[offset + 1 :])
        elif kind == "match_stmt":
            self._walk_expr(node.children[0])
            for branch in node.children[1:]:
                if isinstance(branch, Tree):
                    self._walk_statements(branch.children[1:])
        elif kind == "annotation":
            pass
        # pass/break/continue/breakpoint: nothing to do

    def _enter_loop(self, node: Tree) -> None:
        # A loop that awaits resumes at its top: everything in the body runs
        # after an await on the second iteration.
        for _ in node.find_data("await_expr"):
            self._record_await(_position(node))
            return

    # -- expressions

    # pylint: disable-next=too-many-branches,too-many-return-statements
    # Expression kinds with their own walk; everything else is walked generically.
    _EXPR_HANDLERS = {
        "lambda": "_walk_lambda",
        "await_expr": "_walk_await",
        "standalone_call": "_walk_standalone_call",
        "getattr_call": "_walk_getattr_call",
        "type_test": "_walk_type_test",
        "actual_type_cast": "_walk_type_test",
        "assnmnt_expr": "_walk_assignment",
    }

    def _walk_expr(self, node) -> None:
        if isinstance(node, Token):
            return
        handler = self._EXPR_HANDLERS.get(node.data)
        if handler is not None:
            getattr(self, handler)(node)
            return
        if node.data == "comparison":
            self._check_null_comparison(node)
        elif node.data == "asless_actual_not_test":
            self._check_bare_truthiness(node.children[-1], node)
        elif node.data in ("and_test", "or_test", "asless_and_test", "asless_or_test"):
            for child in node.children:
                self._check_bare_truthiness(child, node)
        for child in node.children:
            self._walk_expr(child)

    def _walk_await(self, node: Tree) -> None:
        # The awaited call runs before the suspension, so its receiver and
        # arguments are checked against the previous await point.
        for child in node.children:
            self._walk_expr(child)
        self._record_await(_position(node))

    def _walk_standalone_call(self, node: Tree) -> None:
        callee = node.children[0]
        if isinstance(callee, Token) and callee.value in GUARD_FUNCTIONS:
            guarded = _single_name(node.children[1]) if len(node.children) > 1 else None
            if guarded is not None:
                self._record_guard(guarded, _position(node))
                return
        self._check_arguments(node.children[1:], node)
        for child in node.children[1:]:
            self._walk_expr(child)

    def _walk_getattr_call(self, node: Tree) -> None:
        getattr_node = node.children[0]
        names = [
            c.value
            for c in getattr_node.children
            if isinstance(c, Token) and c.type == "NAME"
        ]
        if len(names) == 2 and tuple(names) in GUARD_METHODS and len(node.children) > 1:
            guarded = _single_name(node.children[1])
            if guarded is not None:
                self._record_guard(guarded, _position(node))
                return
        receiver, direct = _receiver_name(getattr_node)
        if receiver is not None:
            what = "call a method on" if direct else "call a method through"
            self._check_use(receiver, node, what)
        self._check_arguments(node.children[1:], node)
        for child in node.children:
            self._walk_expr(child)

    def _walk_type_test(self, node: Tree) -> None:
        left = _single_name(node.children[0])
        if left is not None:
            self._check_use(left, node, "type-test")
        for child in node.children:
            self._walk_expr(child)

    def _walk_assignment(self, node: Tree) -> None:
        target = node.children[0]
        for child in node.children[1:]:
            self._walk_expr(child)
        target_name = _single_name(target)
        operator = node.children[1]
        if (
            target_name is not None
            and isinstance(operator, Token)
            and operator.value == "="
        ):
            self._record_assign(target_name, _end_position(node))
        else:
            self._walk_expr(target)

    def _walk_lambda(self, node: Tree) -> None:
        # A lambda runs later by construction: everything it captures is
        # "after an await". Its own parameters and locals are fresh.
        saved_scope = self.scope
        saved_await = self.nearest_await
        self.scope = _Scope(parent=saved_scope)
        self._declare_parameters(node.children[0], self.scope)
        self.nearest_await = _position(node)
        self._walk_statements(node.children[1:])
        self.scope = saved_scope
        self.nearest_await = saved_await

    # -- bookkeeping

    def _record_await(self, position: Position) -> None:
        self.nearest_await = position

    def _record_guard(self, name: str, position: Position) -> None:
        tracked = self.scope.lookup(name)
        if tracked is not None:
            tracked.cleared_at = position

    def _record_assign(self, name: str, position: Position) -> None:
        tracked = self.scope.lookup(name)
        if tracked is not None:
            tracked.cleared_at = position

    def _check_use(
        self, name: str, node: Tree, what: str, rule: str = AWAIT_RULE
    ) -> None:
        run = self.run_await if rule == AWAIT_RULE else self.run_arg
        if not run or self.nearest_await is None or not _is_trackable(name):
            return
        tracked = self.scope.lookup(name)
        if tracked is None or not tracked.node_like or tracked.scene_owned:
            return
        if tracked.declared_at is not None and tracked.declared_at > self.nearest_await:
            return
        if tracked.cleared_at is not None and tracked.cleared_at > self.nearest_await:
            return
        key = (name, _position(node), rule)
        if key in self.reported:
            return
        self.reported.add(key)
        self.problems.append(
            Problem(
                name=rule,
                description=(
                    'Cannot {} "{}" after an await without checking it is still alive: '
                    "it may have been freed while the function was suspended "
                    "(use is_instance_valid() or NodeGuard.is_alive())"
                ).format(what, name),
                line=get_line(node),
                column=get_column(node),
            )
        )

    def _check_arguments(self, arguments, call_node: Tree) -> None:
        # A stale node handed to another function is dereferenced there, out
        # of sight of this rule - the top crash site found in the audit
        # (impostor_capturer.gd) was exactly that shape. Only names declared
        # with a node type are reported: untyped arguments are mostly strings
        # and dictionaries, and typing the parameter is the precise fix.
        for argument in arguments:
            name = _single_name(argument)
            if name is None:
                continue
            tracked = self.scope.lookup(name)
            if tracked is not None and tracked.typed_node and not tracked.scene_owned:
                self._check_use(name, call_node, "pass", ARG_RULE)

    def _check_condition(self, condition) -> None:
        self._check_bare_truthiness(condition, condition)

    def _check_bare_truthiness(self, operand, report_node) -> None:
        if not self.run_null:
            return
        name = _single_name(operand)
        if name is None:
            return
        self._report_null(name, report_node, "truthiness")

    def _check_null_comparison(self, node: Tree) -> None:
        if not self.run_null or len(node.children) != 3:
            return
        left, operator, right = node.children
        if not isinstance(operator, Token) or operator.value not in ("==", "!="):
            return
        for side, other in ((left, right), (right, left)):
            if isinstance(other, Token) and other.value == "null":
                name = _single_name(side)
                if name is not None:
                    self._report_null(name, node, "null comparison")

    def _report_null(self, name: str, node: Tree, what: str) -> None:
        if not _is_trackable(name):
            return
        tracked = self.scope.lookup(name)
        if tracked is None or not tracked.typed_node:
            return
        key = (name, _position(node), NULL_RULE)
        if key in self.reported:
            return
        self.reported.add(key)
        self.problems.append(
            Problem(
                name=NULL_RULE,
                description=(
                    'A {what} on "{name}" does not detect a freed node - a freed '
                    "instance is not null; use is_instance_valid({name})"
                ).format(what=what, name=name),
                line=get_line(node),
                column=get_column(node),
            )
        )
