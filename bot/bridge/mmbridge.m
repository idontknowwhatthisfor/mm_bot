// mmbridge — injected into Mini Motorways to expose the IL2CPP runtime to an external controller.
//
// The game is sandboxed and may only make outgoing connections, so the bridge connects out to a
// controller listening on 127.0.0.1:$MMBRIDGE_PORT. The controller must send the launch token
// ($MMBRIDGE_TOKEN) as its first line. After that, each line is a JSON request answered by one
// JSON line. Requests run on the main (Unity) thread between frames.
//
// Values crossing the wire:
//   numbers/bools/strings/null   primitives, enums (as ints), System.String, Fix64 (as double)
//   {"$t": type, field: ...}     structs, expanded up to `depth` levels
//   {"$h": n, "$t": type, "$p": addr}  managed objects, referenced by handle n until "release"
//   {"$type": "Ns.Name"}          (argument) the System.Type object for a class
//   {"$box": "Ns.Name", "v": x}   (argument) a boxed value type
//   {"$call": {root, path, method, args, sig, then}}  (argument) the object result of a nested call

#import <Foundation/Foundation.h>
#include <arpa/inet.h>
#include <dlfcn.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sys/socket.h>
#include <unistd.h>

typedef struct Il2CppDomain Il2CppDomain;
typedef struct Il2CppAssembly Il2CppAssembly;
typedef struct Il2CppImage Il2CppImage;
typedef struct Il2CppClass Il2CppClass;
typedef struct Il2CppObject Il2CppObject;
typedef struct Il2CppType Il2CppType;
typedef struct FieldInfo FieldInfo;
typedef struct MethodInfo MethodInfo;
typedef struct Il2CppString Il2CppString;
typedef struct Il2CppArray Il2CppArray;
typedef struct Il2CppThread Il2CppThread;

#define IL2CPP_API(X)                                                                              \
    X(Il2CppDomain *, il2cpp_domain_get, (void))                                                   \
    X(const Il2CppAssembly **, il2cpp_domain_get_assemblies, (const Il2CppDomain *, size_t *))     \
    X(const Il2CppImage *, il2cpp_assembly_get_image, (const Il2CppAssembly *))                    \
    X(const char *, il2cpp_image_get_name, (const Il2CppImage *))                                  \
    X(Il2CppClass *, il2cpp_class_from_name, (const Il2CppImage *, const char *, const char *))    \
    X(FieldInfo *, il2cpp_class_get_fields, (Il2CppClass *, void **))                              \
    X(const MethodInfo *, il2cpp_class_get_methods, (Il2CppClass *, void **))                      \
    X(Il2CppClass *, il2cpp_class_get_parent, (Il2CppClass *))                                     \
    X(bool, il2cpp_class_is_valuetype, (const Il2CppClass *))                                      \
    X(bool, il2cpp_class_is_enum, (const Il2CppClass *))                                           \
    X(const Il2CppType *, il2cpp_class_enum_basetype, (Il2CppClass *))                             \
    X(Il2CppClass *, il2cpp_class_from_type, (const Il2CppType *))                                 \
    X(const Il2CppType *, il2cpp_class_get_type, (Il2CppClass *))                                  \
    X(Il2CppClass *, il2cpp_class_get_element_class, (Il2CppClass *))                              \
    X(int32_t, il2cpp_class_value_size, (Il2CppClass *, uint32_t *))                               \
    X(int, il2cpp_array_element_size, (const Il2CppClass *))                                       \
    X(uint32_t, il2cpp_array_length, (Il2CppArray *))                                              \
    X(const char *, il2cpp_field_get_name, (FieldInfo *))                                          \
    X(size_t, il2cpp_field_get_offset, (FieldInfo *))                                              \
    X(const Il2CppType *, il2cpp_field_get_type, (FieldInfo *))                                    \
    X(int, il2cpp_field_get_flags, (FieldInfo *))                                                  \
    X(void, il2cpp_field_static_get_value, (FieldInfo *, void *))                                  \
    X(void, il2cpp_field_static_set_value, (FieldInfo *, void *))                                  \
    X(const char *, il2cpp_method_get_name, (const MethodInfo *))                                  \
    X(uint32_t, il2cpp_method_get_param_count, (const MethodInfo *))                               \
    X(const Il2CppType *, il2cpp_method_get_param, (const MethodInfo *, uint32_t))                 \
    X(const char *, il2cpp_method_get_param_name, (const MethodInfo *, uint32_t))                  \
    X(const Il2CppType *, il2cpp_method_get_return_type, (const MethodInfo *))                     \
    X(uint32_t, il2cpp_method_get_flags, (const MethodInfo *, uint32_t *))                         \
    X(int, il2cpp_type_get_type, (const Il2CppType *))                                             \
    X(char *, il2cpp_type_get_name, (const Il2CppType *))                                          \
    X(bool, il2cpp_type_is_byref, (const Il2CppType *))                                            \
    X(Il2CppObject *, il2cpp_type_get_object, (const Il2CppType *))                                \
    X(Il2CppClass *, il2cpp_object_get_class, (Il2CppObject *))                                    \
    X(Il2CppObject *, il2cpp_object_new, (const Il2CppClass *))                                    \
    X(void *, il2cpp_object_unbox, (Il2CppObject *))                                               \
    X(Il2CppObject *, il2cpp_value_box, (Il2CppClass *, void *))                                   \
    X(Il2CppObject *, il2cpp_runtime_invoke, (const MethodInfo *, void *, void **, Il2CppObject **)) \
    X(void, il2cpp_format_exception, (const Il2CppObject *, char *, int))                          \
    X(Il2CppString *, il2cpp_string_new, (const char *))                                           \
    X(int32_t, il2cpp_string_length, (Il2CppString *))                                             \
    X(uint16_t *, il2cpp_string_chars, (Il2CppString *))                                           \
    X(uint32_t, il2cpp_gchandle_new, (Il2CppObject *, bool))                                       \
    X(Il2CppObject *, il2cpp_gchandle_get_target, (uint32_t))                                      \
    X(void, il2cpp_gchandle_free, (uint32_t))                                                      \
    X(Il2CppThread *, il2cpp_thread_attach, (Il2CppDomain *))                                      \
    X(void, il2cpp_free, (void *))

#define DECLARE_API(ret, name, args) static ret(*name) args;
IL2CPP_API(DECLARE_API)

// Il2CppTypeEnum
enum {
    T_VOID = 0x01, T_BOOLEAN = 0x02, T_CHAR = 0x03, T_I1 = 0x04, T_U1 = 0x05, T_I2 = 0x06,
    T_U2 = 0x07, T_I4 = 0x08, T_U4 = 0x09, T_I8 = 0x0a, T_U8 = 0x0b, T_R4 = 0x0c, T_R8 = 0x0d,
    T_STRING = 0x0e, T_PTR = 0x0f, T_VALUETYPE = 0x11, T_CLASS = 0x12, T_ARRAY = 0x14,
    T_GENERICINST = 0x15, T_I = 0x18, T_U = 0x19, T_FNPTR = 0x1b, T_OBJECT = 0x1c, T_SZARRAY = 0x1d,
};
enum { FIELD_STATIC = 0x10, FIELD_LITERAL = 0x40, METHOD_STATIC = 0x10 };
static const size_t kObjectHeader = 2 * sizeof(void *);  // klass + monitor
static const size_t kArrayData = 4 * sizeof(void *);     // header + bounds + max_length
static const size_t kThreadStaticOffset = (size_t)-1;

#pragma mark - Logging and errors

static void logmsg(NSString *fmt, ...) NS_FORMAT_FUNCTION(1, 2);
static void logmsg(NSString *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    NSLog(@"[mmbridge] %@", [[NSString alloc] initWithFormat:fmt arguments:ap]);
    va_end(ap);
}

static void fail(NSString *fmt, ...) NS_FORMAT_FUNCTION(1, 2) __attribute__((noreturn));
static void fail(NSString *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    NSString *msg = [[NSString alloc] initWithFormat:fmt arguments:ap];
    va_end(ap);
    @throw [NSException exceptionWithName:@"mmbridge" reason:msg userInfo:nil];
}

#pragma mark - Runtime lookup

static BOOL resolveApi(void) {
    static BOOL resolved = NO;
    if (resolved) return YES;
    NSString *path = [[NSBundle mainBundle].privateFrameworksPath
        stringByAppendingPathComponent:@"GameAssembly.dylib"];
    void *lib = dlopen(path.fileSystemRepresentation, RTLD_NOLOAD | RTLD_LAZY);
    if (!lib) return NO;
#define LOAD_API(ret, name, args)                                                                  \
    name = (ret(*) args)dlsym(lib, #name);                                                         \
    if (!name) {                                                                                   \
        logmsg(@"missing export %s", #name);                                                       \
        return NO;                                                                                 \
    }
    IL2CPP_API(LOAD_API)
    resolved = YES;
    return YES;
}

static BOOL runtimeReady(void) {
    if (!resolveApi()) return NO;
    Il2CppDomain *domain = il2cpp_domain_get();
    if (!domain) return NO;
    size_t count = 0;
    il2cpp_domain_get_assemblies(domain, &count);
    return count > 0;
}

static NSString *typeName(const Il2CppType *type) {
    char *raw = il2cpp_type_get_name(type);
    NSString *name = raw ? @(raw) : @"?";
    if (raw) il2cpp_free(raw);
    return name;
}

static NSString *className(Il2CppClass *klass) {
    static NSMutableDictionary<NSNumber *, NSString *> *cache;
    if (!cache) cache = [NSMutableDictionary new];
    NSNumber *key = @((uintptr_t)klass);
    NSString *name = cache[key];
    if (!name) cache[key] = name = typeName(il2cpp_class_get_type(klass));
    return name;
}

static Il2CppClass *findClass(NSString *fullName) {
    static NSMutableDictionary<NSString *, NSValue *> *cache;
    if (!cache) cache = [NSMutableDictionary new];
    NSValue *hit = cache[fullName];
    if (hit) return hit.pointerValue;

    NSRange dot = [fullName rangeOfString:@"." options:NSBackwardsSearch];
    NSString *ns = dot.location == NSNotFound ? @"" : [fullName substringToIndex:dot.location];
    NSString *name = dot.location == NSNotFound ? fullName : [fullName substringFromIndex:dot.location + 1];
    size_t count = 0;
    const Il2CppAssembly **assemblies = il2cpp_domain_get_assemblies(il2cpp_domain_get(), &count);
    for (size_t i = 0; i < count; i++) {
        const Il2CppImage *image = il2cpp_assembly_get_image(assemblies[i]);
        Il2CppClass *klass = il2cpp_class_from_name(image, ns.UTF8String, name.UTF8String);
        if (klass) {
            cache[fullName] = [NSValue valueWithPointer:klass];
            return klass;
        }
    }
    fail(@"class not found: %@", fullName);
}

static BOOL isValueType(const Il2CppType *type) {
    switch (il2cpp_type_get_type(type)) {
        case T_BOOLEAN: case T_CHAR: case T_I1: case T_U1: case T_I2: case T_U2: case T_I4:
        case T_U4: case T_I8: case T_U8: case T_R4: case T_R8: case T_I: case T_U:
        case T_PTR: case T_FNPTR: case T_VALUETYPE:
            return YES;
        case T_GENERICINST:
            return il2cpp_class_is_valuetype(il2cpp_class_from_type(type));
        default:
            return NO;
    }
}

static size_t valueSize(Il2CppClass *klass) {
    uint32_t align = 0;
    return (size_t)il2cpp_class_value_size(klass, &align);
}

// "<Name>k__BackingField" -> "Name"
static NSString *cleanFieldName(const char *raw) {
    NSString *name = @(raw);
    if ([name hasPrefix:@"<"] && [name hasSuffix:@">k__BackingField"])
        return [name substringWithRange:NSMakeRange(1, name.length - 1 - @">k__BackingField".length)];
    return name;
}

static FieldInfo *findField(Il2CppClass *klass, NSString *name) {
    static NSMutableDictionary<NSString *, NSValue *> *cache;
    if (!cache) cache = [NSMutableDictionary new];
    NSString *key = [NSString stringWithFormat:@"%p/%@", klass, name];
    NSValue *hit = cache[key];
    if (hit) return hit.pointerValue;
    FieldInfo *found = NULL;
    for (Il2CppClass *k = klass; k && !found; k = il2cpp_class_get_parent(k)) {
        void *iter = NULL;
        FieldInfo *field;
        while ((field = il2cpp_class_get_fields(k, &iter))) {
            const char *raw = il2cpp_field_get_name(field);
            if ([name isEqualToString:@(raw)] || [name isEqualToString:cleanFieldName(raw)]) {
                found = field;
                break;
            }
        }
    }
    cache[key] = [NSValue valueWithPointer:found];
    return found;
}

static NSString *signatureOf(const MethodInfo *method) {
    NSMutableArray *params = [NSMutableArray new];
    for (uint32_t i = 0; i < il2cpp_method_get_param_count(method); i++)
        [params addObject:typeName(il2cpp_method_get_param(method, i))];
    return [params componentsJoinedByString:@","];
}

// Most-derived first, so virtual overrides win over the declarations they override.
static const MethodInfo *findMethodUncached(Il2CppClass *klass, NSString *name, NSInteger argc, NSArray *sig) {
    NSString *wantSig = [sig componentsJoinedByString:@","];
    for (Il2CppClass *k = klass; k; k = il2cpp_class_get_parent(k)) {
        void *iter = NULL;
        const MethodInfo *method;
        while ((method = il2cpp_class_get_methods(k, &iter))) {
            if (![name isEqualToString:@(il2cpp_method_get_name(method))]) continue;
            if (argc >= 0 && il2cpp_method_get_param_count(method) != (uint32_t)argc) continue;
            if (wantSig && ![wantSig isEqualToString:signatureOf(method)]) continue;
            return method;
        }
    }
    return NULL;
}

static const MethodInfo *findMethod(Il2CppClass *klass, NSString *name, NSInteger argc, NSArray *sig) {
    static NSMutableDictionary<NSString *, NSValue *> *cache;
    if (!cache) cache = [NSMutableDictionary new];
    NSString *key = [NSString stringWithFormat:@"%p/%@/%ld/%@", klass, name, (long)argc,
                                               [sig componentsJoinedByString:@","] ?: @"*"];
    NSValue *hit = cache[key];
    if (hit) return hit.pointerValue;
    const MethodInfo *found = findMethodUncached(klass, name, argc, sig);
    cache[key] = [NSValue valueWithPointer:found];
    return found;
}

#pragma mark - Values

typedef enum { V_NULL, V_OBJ, V_VAL, V_CLASS } VKind;
typedef struct {
    VKind kind;
    Il2CppClass *klass;  // runtime class (V_OBJ), value class (V_VAL), or the class itself (V_CLASS)
    Il2CppObject *obj;   // V_OBJ
    void *data;          // V_VAL: unboxed value storage
} Val;

static Val vnull(void) { return (Val){V_NULL, NULL, NULL, NULL}; }
static Val vobj(Il2CppObject *obj) {
    return obj ? (Val){V_OBJ, il2cpp_object_get_class(obj), obj, NULL} : vnull();
}
static Val vval(Il2CppClass *klass, void *data) { return (Val){V_VAL, klass, NULL, data}; }
static NSValue *boxVal(Val v) { return [NSValue valueWithBytes:&v objCType:@encode(Val)]; }
static Val unboxVal(NSValue *value) {
    Val v;
    [value getValue:&v size:sizeof v];
    return v;
}

static Val valAt(void *addr, const Il2CppType *type) {
    if (isValueType(type)) return vval(il2cpp_class_from_type(type), addr);
    return vobj(*(Il2CppObject **)addr);
}

// Scratch memory that lives until the current request finishes.
static void *scratch(NSMutableArray *keep, size_t size) {
    NSMutableData *buf = [NSMutableData dataWithLength:MAX(size, 16)];
    [keep addObject:buf];
    return buf.mutableBytes;
}

static void *fieldAddress(Val owner, FieldInfo *field, NSMutableArray *keep) {
    int flags = il2cpp_field_get_flags(field);
    const Il2CppType *type = il2cpp_field_get_type(field);
    if (flags & FIELD_LITERAL) fail(@"%s is a constant", il2cpp_field_get_name(field));
    if (flags & FIELD_STATIC) {
        size_t size = isValueType(type) ? valueSize(il2cpp_class_from_type(type)) : sizeof(void *);
        void *buf = scratch(keep, size);
        il2cpp_field_static_get_value(field, buf);
        return buf;
    }
    size_t offset = il2cpp_field_get_offset(field);
    if (offset == kThreadStaticOffset) fail(@"%s is thread-static", il2cpp_field_get_name(field));
    switch (owner.kind) {
        case V_OBJ: return (char *)owner.obj + offset;
        case V_VAL: return (char *)owner.data + offset - kObjectHeader;
        default: fail(@"instance field %s needs an instance", il2cpp_field_get_name(field));
    }
}

static NSMutableArray<NSNumber *> *gHandles;
static BOOL gMakeHandles = YES;  // per request; when NO, objects are reported by type and address only

static NSDictionary *handleFor(Il2CppObject *obj) {
    if (!gMakeHandles)
        return @{@"$t": className(il2cpp_object_get_class(obj)), @"$p": @((uintptr_t)obj)};
    [gHandles addObject:@(il2cpp_gchandle_new(obj, false))];
    return @{@"$h": @(gHandles.count - 1), @"$t": className(il2cpp_object_get_class(obj)),
             @"$p": @((uintptr_t)obj)};
}

// Frees every handle from index `keep` on, so long-lived roots can be pinned at the front.
static void releaseHandles(NSUInteger keep) {
    while (gHandles.count > keep) {
        il2cpp_gchandle_free(gHandles.lastObject.unsignedIntValue);
        [gHandles removeLastObject];
    }
}

static id toJSON(Val v, int depth);

static id valueToJSON(Il2CppClass *klass, void *data, int depth) {
    int kind = il2cpp_type_get_type(il2cpp_class_get_type(klass));
    if (il2cpp_class_is_enum(klass)) kind = il2cpp_type_get_type(il2cpp_class_enum_basetype(klass));
    switch (kind) {
        case T_BOOLEAN: return @(*(uint8_t *)data != 0);
        case T_CHAR: case T_U2: return @(*(uint16_t *)data);
        case T_I1: return @(*(int8_t *)data);
        case T_U1: return @(*(uint8_t *)data);
        case T_I2: return @(*(int16_t *)data);
        case T_I4: return @(*(int32_t *)data);
        case T_U4: return @(*(uint32_t *)data);
        case T_I8: return @(*(int64_t *)data);
        case T_U8: return @(*(uint64_t *)data);
        case T_R4: return @(*(float *)data);
        case T_R8: return @(*(double *)data);
        case T_I: case T_U: case T_PTR: case T_FNPTR: return @(*(intptr_t *)data);
        default: break;
    }
    NSString *name = className(klass);
    if ([name isEqualToString:@"FixMath.Fix64"]) return @((double)*(int64_t *)data / 4294967296.0);
    if (depth <= 0) return @{@"$t": name, @"$trunc": @YES};
    NSMutableDictionary *out = [NSMutableDictionary dictionaryWithObject:name forKey:@"$t"];
    void *iter = NULL;
    FieldInfo *field;
    while ((field = il2cpp_class_get_fields(klass, &iter))) {
        if (il2cpp_field_get_flags(field) & (FIELD_STATIC | FIELD_LITERAL)) continue;
        void *addr = (char *)data + il2cpp_field_get_offset(field) - kObjectHeader;
        out[cleanFieldName(il2cpp_field_get_name(field))] =
            toJSON(valAt(addr, il2cpp_field_get_type(field)), depth - 1);
    }
    return out;
}

static id toJSON(Val v, int depth) {
    switch (v.kind) {
        case V_NULL: return NSNull.null;
        case V_CLASS: return @{@"$class": className(v.klass)};
        case V_VAL: return valueToJSON(v.klass, v.data, depth);
        case V_OBJ:
            if (il2cpp_type_get_type(il2cpp_class_get_type(v.klass)) == T_STRING) {
                Il2CppString *s = (Il2CppString *)v.obj;
                return [NSString stringWithCharacters:il2cpp_string_chars(s) length:il2cpp_string_length(s)];
            }
            if (il2cpp_class_is_valuetype(v.klass))
                return valueToJSON(v.klass, il2cpp_object_unbox(v.obj), depth);
            return handleFor(v.obj);
    }
}

#pragma mark - Collections

static int intField(Val owner, NSString *name, NSMutableArray *keep) {
    FieldInfo *field = findField(owner.klass, name);
    if (!field) fail(@"%@ has no field %@", className(owner.klass), name);
    return *(int32_t *)fieldAddress(owner, field, keep);
}

static Val fieldVal(Val owner, NSString *name, NSMutableArray *keep) {
    FieldInfo *field = findField(owner.klass, name);
    if (!field) fail(@"%@ has no field %@", className(owner.klass), name);
    return valAt(fieldAddress(owner, field, keep), il2cpp_field_get_type(field));
}

static NSMutableArray<NSValue *> *arrayElements(Val array, NSUInteger limit) {
    Il2CppClass *elementClass = il2cpp_class_get_element_class(array.klass);
    const Il2CppType *elementType = il2cpp_class_get_type(elementClass);
    size_t stride = (size_t)il2cpp_array_element_size(array.klass);
    NSUInteger count = MIN(limit, (NSUInteger)il2cpp_array_length((Il2CppArray *)array.obj));
    char *base = (char *)array.obj + kArrayData;
    NSMutableArray *out = [NSMutableArray arrayWithCapacity:count];
    for (NSUInteger i = 0; i < count; i++) [out addObject:boxVal(valAt(base + i * stride, elementType))];
    return out;
}

// Elements of arrays, List<T>, HashSet<T> (values) and Dictionary<K,V> (entries with key/value).
static NSArray<NSValue *> *elements(Val v, NSMutableArray *keep) {
    if (v.kind != V_OBJ) fail(@"cannot iterate a non-object");
    int kind = il2cpp_type_get_type(il2cpp_class_get_type(v.klass));
    if (kind == T_SZARRAY || kind == T_ARRAY) return arrayElements(v, NSUIntegerMax);
    if (findField(v.klass, @"_items") && findField(v.klass, @"_size")) {  // List<T>
        Val items = fieldVal(v, @"_items", keep);
        return items.kind == V_NULL ? @[] : arrayElements(items, intField(v, @"_size", keep));
    }
    if (findField(v.klass, @"_array") && findField(v.klass, @"_size")) {  // Stack<T>, Queue<T>
        Val array = fieldVal(v, @"_array", keep);
        if (array.kind == V_NULL) return @[];
        NSUInteger size = (NSUInteger)intField(v, @"_size", keep);
        if (!findField(v.klass, @"_head")) {  // Stack: bottom to top
            return arrayElements(array, size);
        }
        NSArray *all = arrayElements(array, NSUIntegerMax);  // Queue: ring buffer from _head
        NSUInteger head = (NSUInteger)intField(v, @"_head", keep);
        NSMutableArray *out = [NSMutableArray arrayWithCapacity:size];
        for (NSUInteger i = 0; i < size && all.count; i++) [out addObject:all[(head + i) % all.count]];
        return out;
    }
    if (findField(v.klass, @"_entries") && findField(v.klass, @"_count")) {
        Val entries = fieldVal(v, @"_entries", keep);
        if (entries.kind == V_NULL) return @[];
        NSMutableArray *out = [NSMutableArray new];
        for (NSValue *e in arrayElements(entries, intField(v, @"_count", keep)))
            if (intField(unboxVal(e), @"hashCode", keep) >= 0) [out addObject:e];
        return out;
    }
    if (findField(v.klass, @"_slots") && findField(v.klass, @"_lastIndex")) {
        Val slots = fieldVal(v, @"_slots", keep);
        if (slots.kind == V_NULL) return @[];
        NSMutableArray *out = [NSMutableArray new];
        for (NSValue *s in arrayElements(slots, intField(v, @"_lastIndex", keep)))
            if (intField(unboxVal(s), @"hashCode", keep) >= 0)
                [out addObject:boxVal(fieldVal(unboxVal(s), @"value", keep))];
        return out;
    }
    // Wrapper types (ObserverList, ModelList...) usually keep a List<T> or T[] inside.
    void *iter = NULL;
    FieldInfo *field;
    while ((field = il2cpp_class_get_fields(v.klass, &iter))) {
        if (il2cpp_field_get_flags(field) & FIELD_STATIC) continue;
        NSString *type = typeName(il2cpp_field_get_type(field));
        if ([type hasPrefix:@"System.Collections.Generic.List<"] || [type hasSuffix:@"[]"]) {
            Val inner = valAt(fieldAddress(v, field, keep), il2cpp_field_get_type(field));
            return inner.kind == V_NULL ? @[] : elements(inner, keep);
        }
    }
    fail(@"%@ is not iterable", className(v.klass));
}

#pragma mark - Marshalling arguments

static Il2CppObject *objectFromJSON(id arg, NSMutableArray *keep);
static Val invoke(Val target, NSString *name, NSArray *args, NSArray *sig, NSMutableArray *keep);
static Val rootFromJSON(id spec, NSMutableArray *keep);
static Val walkToVal(Val v, NSArray *steps, NSMutableArray *keep);

static void writeValue(void *dst, Il2CppClass *klass, id arg, NSMutableArray *keep) {
    int kind = il2cpp_type_get_type(il2cpp_class_get_type(klass));
    if (il2cpp_class_is_enum(klass)) kind = il2cpp_type_get_type(il2cpp_class_enum_basetype(klass));
    switch (kind) {
        case T_BOOLEAN: *(uint8_t *)dst = [arg boolValue]; return;
        case T_I1: case T_U1: *(int8_t *)dst = (int8_t)[arg intValue]; return;
        case T_CHAR: case T_I2: case T_U2: *(int16_t *)dst = (int16_t)[arg intValue]; return;
        case T_I4: case T_U4: *(int32_t *)dst = (int32_t)[arg longLongValue]; return;
        case T_I8: case T_U8: case T_I: case T_U: case T_PTR: *(int64_t *)dst = [arg longLongValue]; return;
        case T_R4: *(float *)dst = [arg floatValue]; return;
        case T_R8: *(double *)dst = [arg doubleValue]; return;
        default: break;
    }
    NSString *name = className(klass);
    if ([name isEqualToString:@"FixMath.Fix64"]) {
        *(int64_t *)dst = llround([arg doubleValue] * 4294967296.0);
        return;
    }
    if ([name hasPrefix:@"System.Nullable<"]) {
        memset(dst, 0, valueSize(klass));
        if (arg == nil || arg == NSNull.null) return;
        *(uint8_t *)fieldAddress(vval(klass, dst), findField(klass, @"hasValue"), keep) = 1;
        FieldInfo *value = findField(klass, @"value");
        writeValue(fieldAddress(vval(klass, dst), value, keep),
                   il2cpp_class_from_type(il2cpp_field_get_type(value)), arg, keep);
        return;
    }
    if (![arg isKindOfClass:NSDictionary.class]) fail(@"%@ needs an object argument", name);
    void *iter = NULL;
    FieldInfo *field;
    while ((field = il2cpp_class_get_fields(klass, &iter))) {
        if (il2cpp_field_get_flags(field) & (FIELD_STATIC | FIELD_LITERAL)) continue;
        NSString *fname = cleanFieldName(il2cpp_field_get_name(field));
        id value = arg[fname];
        if (!value && [fname hasPrefix:@"m_"]) {  // Unity's m_X also accepts x / X
            NSString *bare = [fname substringFromIndex:2];
            value = arg[bare] ?: arg[bare.lowercaseString];
        }
        if (!value) continue;
        const Il2CppType *ftype = il2cpp_field_get_type(field);
        void *addr = (char *)dst + il2cpp_field_get_offset(field) - kObjectHeader;
        if (isValueType(ftype))
            writeValue(addr, il2cpp_class_from_type(ftype), value, keep);
        else
            *(Il2CppObject **)addr = objectFromJSON(value, keep);
    }
}

static Il2CppObject *objectFromJSON(id arg, NSMutableArray *keep) {
    if (arg == nil || arg == NSNull.null) return NULL;
    if ([arg isKindOfClass:NSString.class]) return (Il2CppObject *)il2cpp_string_new([arg UTF8String]);
    if ([arg isKindOfClass:NSDictionary.class]) {
        if (arg[@"$h"]) {
            NSUInteger index = [arg[@"$h"] unsignedIntegerValue];
            if (index >= gHandles.count) fail(@"stale handle %lu", (unsigned long)index);
            return il2cpp_gchandle_get_target(gHandles[index].unsignedIntValue);
        }
        if (arg[@"$type"]) return il2cpp_type_get_object(il2cpp_class_get_type(findClass(arg[@"$type"])));
        if (arg[@"$call"]) {  // nested call; its (object) result becomes the argument
            NSDictionary *c = arg[@"$call"];
            Val target = walkToVal(rootFromJSON(c[@"root"], keep), c[@"path"] ?: @[], keep);
            Val ret = invoke(target, c[@"method"], c[@"args"] ?: @[], c[@"sig"], keep);
            ret = walkToVal(ret, c[@"then"] ?: @[], keep);
            if (ret.kind == V_NULL) fail(@"nested call %@ returned null", c[@"method"]);
            if (ret.kind != V_OBJ) fail(@"nested call %@ did not return an object", c[@"method"]);
            return ret.obj;
        }
        if (arg[@"$box"]) {
            Il2CppClass *klass = findClass(arg[@"$box"]);
            void *buf = scratch(keep, valueSize(klass));
            writeValue(buf, klass, arg[@"v"], keep);
            return il2cpp_value_box(klass, buf);
        }
    }
    fail(@"cannot convert %@ to an object", arg);
}

static NSMutableArray *gLastOuts;

static Val invoke(Val target, NSString *name, NSArray *args, NSArray *sig, NSMutableArray *keep) {
    if (target.kind == V_NULL) fail(@"cannot call %@ on null", name);
    const MethodInfo *method = findMethod(target.klass, name, (NSInteger)args.count, sig);
    if (!method) fail(@"%@ has no method %@ taking %lu args", className(target.klass), name,
                      (unsigned long)args.count);
    uint32_t implFlags = 0;
    BOOL isStatic = (il2cpp_method_get_flags(method, &implFlags) & METHOD_STATIC) != 0;
    void *thisArg = NULL;
    if (!isStatic) {
        if (target.kind == V_OBJ) thisArg = target.obj;
        else if (target.kind == V_VAL) thisArg = il2cpp_value_box(target.klass, target.data);
        else fail(@"%@ is an instance method", name);
    }

    void **params = scratch(keep, args.count * sizeof(void *));
    NSMutableArray *outSlots = [NSMutableArray new];
    for (NSUInteger i = 0; i < args.count; i++) {
        const Il2CppType *ptype = il2cpp_method_get_param(method, (uint32_t)i);
        Il2CppClass *pclass = il2cpp_class_from_type(ptype);
        BOOL byValue = il2cpp_class_is_valuetype(pclass);
        if (il2cpp_type_is_byref(ptype)) {
            void *slot = scratch(keep, byValue ? valueSize(pclass) : sizeof(void *));
            if (args[i] != NSNull.null) {
                if (byValue) writeValue(slot, pclass, args[i], keep);
                else *(Il2CppObject **)slot = objectFromJSON(args[i], keep);
            }
            params[i] = slot;
            [outSlots addObject:@[@(i), [NSValue valueWithPointer:slot], [NSValue valueWithPointer:pclass]]];
        } else if (byValue) {
            void *slot = scratch(keep, valueSize(pclass));
            writeValue(slot, pclass, args[i], keep);
            params[i] = slot;
        } else {
            params[i] = objectFromJSON(args[i], keep);
        }
    }

    Il2CppObject *exception = NULL;
    Il2CppObject *ret = il2cpp_runtime_invoke(method, thisArg, params, &exception);
    if (exception) {
        char message[4096] = {0};
        il2cpp_format_exception(exception, message, sizeof message);
        fail(@"%@ threw: %s", name, message);
    }
    gLastOuts = [NSMutableArray new];
    for (NSArray *out in outSlots) {
        Il2CppClass *pclass = [out[2] pointerValue];
        [gLastOuts addObject:toJSON(valAt([out[1] pointerValue], il2cpp_class_get_type(pclass)), 4)];
    }
    if (il2cpp_type_get_type(il2cpp_method_get_return_type(method)) == T_VOID) return vnull();
    return vobj(ret);
}

#pragma mark - Paths

static Val rootFromJSON(id spec, NSMutableArray *keep) {
    if (spec == nil || spec == NSNull.null) fail(@"missing root");
    if ([spec isKindOfClass:NSDictionary.class] && spec[@"$class"])
        return (Val){V_CLASS, findClass(spec[@"$class"]), NULL, NULL};
    return vobj(objectFromJSON(spec, keep));
}

static Val step(Val v, NSString *name, NSMutableArray *keep) {
    if (v.kind == V_NULL) return v;
    if ([name hasPrefix:@"?"]) {  // type filter: continue only if the class name ends with the rest
        NSString *want = [name substringFromIndex:1];
        NSString *have = className(v.klass);
        BOOL match = [have isEqualToString:want] || [have hasSuffix:[@"." stringByAppendingString:want]];
        return match ? v : vnull();
    }
    if ([name hasPrefix:@"@"]) return invoke(v, [name substringFromIndex:1], @[], nil, keep);
    if ([name hasPrefix:@"#"]) {
        NSArray *items = elements(v, keep);
        NSInteger index = [name substringFromIndex:1].integerValue;
        if (index < 0) index += (NSInteger)items.count;
        if (index < 0 || index >= (NSInteger)items.count) fail(@"index %@ out of range", name);
        return unboxVal(items[(NSUInteger)index]);
    }
    FieldInfo *field = findField(v.klass, name);
    if (field) return valAt(fieldAddress(v, field, keep), il2cpp_field_get_type(field));
    NSString *getter = [@"get_" stringByAppendingString:name];
    if (findMethod(v.klass, getter, 0, nil)) return invoke(v, getter, @[], nil, keep);
    fail(@"%@ has no field or property %@", className(v.klass), name);
}

// Steps: field/property name, "@Method" (zero-arg call), "#i" (element), "*" (map over elements),
// "?Type" (null unless the value's class is Type, matched on the trailing name),
// or an object {"key": [steps...]} projecting several sub-paths (must be the last step).
static id walk(Val v, NSArray *steps, NSUInteger from, int depth, NSMutableArray *keep) {
    for (NSUInteger i = from; i < steps.count; i++) {
        id s = steps[i];
        if ([s isKindOfClass:NSDictionary.class]) {
            if (v.kind == V_NULL) return NSNull.null;
            NSMutableDictionary *out = [NSMutableDictionary new];
            for (NSString *key in s) {
                @try {
                    out[key] = walk(v, s[key], 0, depth, keep);
                } @catch (NSException *e) {
                    out[key] = @{@"$err": e.reason ?: e.name};
                }
            }
            return out;
        }
        if ([s isEqual:@"*"]) {
            if (v.kind == V_NULL) return NSNull.null;
            NSMutableArray *out = [NSMutableArray new];
            for (NSValue *item in elements(v, keep)) [out addObject:walk(unboxVal(item), steps, i + 1, depth, keep)];
            return out;
        }
        v = step(v, s, keep);
    }
    return toJSON(v, depth);
}

static Val walkToVal(Val v, NSArray *steps, NSMutableArray *keep) {
    for (NSString *s in steps) v = step(v, s, keep);
    return v;
}

#pragma mark - Requests

static NSArray *describeFields(Il2CppClass *klass) {
    NSMutableArray *out = [NSMutableArray new];
    void *iter = NULL;
    FieldInfo *field;
    while ((field = il2cpp_class_get_fields(klass, &iter))) {
        int flags = il2cpp_field_get_flags(field);
        [out addObject:@{@"name": @(il2cpp_field_get_name(field)),
                         @"type": typeName(il2cpp_field_get_type(field)),
                         @"offset": @(il2cpp_field_get_offset(field)),
                         @"static": @((flags & FIELD_STATIC) != 0),
                         @"const": @((flags & FIELD_LITERAL) != 0)}];
    }
    return out;
}

static NSArray *describeMethods(Il2CppClass *klass) {
    NSMutableArray *out = [NSMutableArray new];
    void *iter = NULL;
    const MethodInfo *method;
    while ((method = il2cpp_class_get_methods(klass, &iter))) {
        NSMutableArray *params = [NSMutableArray new];
        for (uint32_t i = 0; i < il2cpp_method_get_param_count(method); i++)
            [params addObject:@[typeName(il2cpp_method_get_param(method, i)),
                                @(il2cpp_method_get_param_name(method, i) ?: "")]];
        uint32_t implFlags = 0;
        [out addObject:@{@"name": @(il2cpp_method_get_name(method)), @"params": params,
                         @"ret": typeName(il2cpp_method_get_return_type(method)),
                         @"static": @((il2cpp_method_get_flags(method, &implFlags) & METHOD_STATIC) != 0)}];
    }
    return out;
}

static id handleRequest(NSDictionary *req, NSMutableArray *keep);

static id handleOp(NSDictionary *req, NSMutableArray *keep) {
    NSString *op = req[@"op"];
    int depth = req[@"depth"] ? [req[@"depth"] intValue] : 4;
    NSArray *path = req[@"path"] ?: @[];

    if ([op isEqual:@"ping"]) return @{@"pong": @YES, @"handles": @(gHandles.count)};
    if ([op isEqual:@"release"]) {
        releaseHandles([req[@"keep"] unsignedIntegerValue]);
        return @(gHandles.count);
    }
    if ([op isEqual:@"batch"]) {
        NSMutableArray *out = [NSMutableArray new];
        for (NSDictionary *sub in req[@"requests"]) [out addObject:handleRequest(sub, keep)];
        return out;
    }
    if ([op isEqual:@"class"]) {
        Il2CppClass *klass = findClass(req[@"name"]);
        NSMutableArray *chain = [NSMutableArray new];
        for (Il2CppClass *k = klass; k; k = il2cpp_class_get_parent(k)) {
            [chain addObject:@{@"name": className(k), @"fields": describeFields(k),
                               @"methods": describeMethods(k)}];
            if (![req[@"inherited"] boolValue]) break;
        }
        return chain;
    }
    if ([op isEqual:@"describe"]) {  // class layout of the object at root+path
        Val v = walkToVal(rootFromJSON(req[@"root"], keep), path, keep);
        if (v.kind == V_NULL) return NSNull.null;
        return @{@"name": className(v.klass), @"fields": describeFields(v.klass)};
    }
    if ([op isEqual:@"find_objects"]) {
        Val unityObject = (Val){V_CLASS, findClass(@"UnityEngine.Object"), NULL, NULL};
        Val array = invoke(unityObject, @"FindObjectsOfType", @[@{@"$type": req[@"class"]}],
                           @[@"System.Type"], keep);
        return walk(array, @[@"*"], 0, depth, keep);
    }
    if ([op isEqual:@"find_all"]) {  // includes assets such as ScriptableObjects
        Val resources = (Val){V_CLASS, findClass(@"UnityEngine.Resources"), NULL, NULL};
        Val array = invoke(resources, @"FindObjectsOfTypeAll", @[@{@"$type": req[@"class"]}],
                           @[@"System.Type"], keep);
        return walk(array, @[@"*"], 0, depth, keep);
    }
    if ([op isEqual:@"new"]) {
        Il2CppClass *klass = findClass(req[@"class"]);
        Il2CppObject *obj = il2cpp_object_new(klass);
        invoke(vobj(obj), @".ctor", req[@"args"] ?: @[], req[@"sig"], keep);
        return toJSON(vobj(obj), depth);
    }
    if ([op isEqual:@"get"]) return walk(rootFromJSON(req[@"root"], keep), path, 0, depth, keep);
    if ([op isEqual:@"set"]) {
        if (path.count == 0) fail(@"set needs a path");
        Val owner = walkToVal(rootFromJSON(req[@"root"], keep), [path subarrayWithRange:NSMakeRange(0, path.count - 1)], keep);
        if (owner.kind == V_NULL) fail(@"set on null");
        FieldInfo *field = findField(owner.klass, path.lastObject);
        if (!field) fail(@"%@ has no field %@", className(owner.klass), path.lastObject);
        const Il2CppType *type = il2cpp_field_get_type(field);
        BOOL byValue = isValueType(type);
        void *buf = scratch(keep, byValue ? valueSize(il2cpp_class_from_type(type)) : sizeof(void *));
        if (byValue) writeValue(buf, il2cpp_class_from_type(type), req[@"value"], keep);
        else *(Il2CppObject **)buf = objectFromJSON(req[@"value"], keep);
        if (il2cpp_field_get_flags(field) & FIELD_STATIC) {
            il2cpp_field_static_set_value(field, byValue ? buf : *(void **)buf);
        } else {
            void *addr = fieldAddress(owner, field, keep);
            memcpy(addr, buf, byValue ? valueSize(il2cpp_class_from_type(type)) : sizeof(void *));
        }
        return @YES;
    }
    if ([op isEqual:@"call_many"]) {
        Val target = walkToVal(rootFromJSON(req[@"root"], keep), path, keep);
        NSMutableArray *out = [NSMutableArray new];
        for (NSArray *args in req[@"args_list"]) {
            @try {
                Val ret = invoke(target, req[@"method"], args, req[@"sig"], keep);
                [out addObject:walk(ret, req[@"then"] ?: @[], 0, depth, keep)];
            } @catch (NSException *e) {
                [out addObject:@{@"$err": e.reason ?: e.name}];
            }
        }
        return out;
    }
    if ([op isEqual:@"call"]) {
        Val target = walkToVal(rootFromJSON(req[@"root"], keep), path, keep);
        gLastOuts = nil;
        Val ret = invoke(target, req[@"method"], req[@"args"] ?: @[], req[@"sig"], keep);
        id result = walk(ret, req[@"then"] ?: @[], 0, depth, keep);
        return gLastOuts.count ? @{@"$ret": result, @"$outs": gLastOuts} : result;
    }
    fail(@"unknown op %@", op);
}

static id handleRequest(NSDictionary *req, NSMutableArray *keep) {
    BOOL savedMakeHandles = gMakeHandles;
    if (req[@"handles"]) gMakeHandles = [req[@"handles"] boolValue];
    @try {
        return @{@"ok": handleOp(req, keep) ?: NSNull.null};
    } @catch (NSException *e) {
        return @{@"error": e.reason ?: e.name};
    } @finally {
        gMakeHandles = savedMakeHandles;
    }
}

static id runOnMainThread(NSDictionary *req) {
    NSMutableDictionary *box = [NSMutableDictionary new];
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    dispatch_async(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            NSMutableArray *keep = [NSMutableArray new];
            box[@"r"] = handleRequest(req, keep);
        }
        dispatch_semaphore_signal(done);
    });
    if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC)))
        return @{@"error": @"timed out waiting for the main thread"};
    return box[@"r"];
}

#pragma mark - Connection

static BOOL sendLine(int fd, id object) {
    NSError *error = nil;
    NSData *json = [NSJSONSerialization dataWithJSONObject:object options:0 error:&error];
    if (!json) {
        logmsg(@"cannot encode response: %@", error);
        json = [@"{\"error\":\"response is not JSON-encodable\"}" dataUsingEncoding:NSUTF8StringEncoding];
    }
    NSMutableData *line = [json mutableCopy];
    [line appendBytes:"\n" length:1];
    const char *bytes = line.bytes;
    size_t left = line.length;
    while (left > 0) {
        ssize_t n = send(fd, bytes, left, 0);
        if (n <= 0) return NO;
        bytes += n;
        left -= (size_t)n;
    }
    return YES;
}

static void serve(int fd, NSString *token) {
    NSMutableData *buffer = [NSMutableData new];
    BOOL authenticated = NO;
    char chunk[65536];
    for (;;) {
        ssize_t n = recv(fd, chunk, sizeof chunk, 0);
        if (n <= 0) return;
        [buffer appendBytes:chunk length:(NSUInteger)n];
        for (;;) {
            NSRange nl = [buffer rangeOfData:[NSData dataWithBytes:"\n" length:1] options:0
                                       range:NSMakeRange(0, buffer.length)];
            if (nl.location == NSNotFound) break;
            @autoreleasepool {
                NSData *line = [buffer subdataWithRange:NSMakeRange(0, nl.location)];
                [buffer replaceBytesInRange:NSMakeRange(0, nl.location + 1) withBytes:NULL length:0];
                NSDictionary *req = [NSJSONSerialization JSONObjectWithData:line options:0 error:nil];
                if (![req isKindOfClass:NSDictionary.class]) {
                    if (!sendLine(fd, @{@"error": @"bad JSON"})) return;
                    continue;
                }
                if (!authenticated) {
                    if (![req[@"token"] isEqual:token]) {
                        logmsg(@"rejected controller with a bad token");
                        return;
                    }
                    authenticated = YES;
                    sendLine(fd, @{@"hello": @"mmbridge", @"pid": @(getpid()), @"ready": @(runtimeReady())});
                    continue;
                }
                id response = runtimeReady() ? runOnMainThread(req) : @{@"error": @"il2cpp not ready"};
                NSMutableDictionary *tagged = [response mutableCopy];
                if (req[@"id"]) tagged[@"id"] = req[@"id"];
                if (!sendLine(fd, tagged)) return;
            }
        }
    }
}

static void *connectLoop(void *unused) {
    (void)unused;
    const char *tokenEnv = getenv("MMBRIDGE_TOKEN");
    const char *portEnv = getenv("MMBRIDGE_PORT");
    if (!tokenEnv || !*tokenEnv) {
        logmsg(@"MMBRIDGE_TOKEN not set; bridge disabled");
        return NULL;
    }
    NSString *token = @(tokenEnv);
    uint16_t port = portEnv ? (uint16_t)atoi(portEnv) : 47811;
    gHandles = [NSMutableArray new];
    logmsg(@"loaded; connecting to 127.0.0.1:%u", port);
    for (;;) {
        @autoreleasepool {
            int fd = socket(AF_INET, SOCK_STREAM, 0);
            struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(port),
                                       .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
            if (fd >= 0 && connect(fd, (struct sockaddr *)&addr, sizeof addr) == 0) {
                int one = 1;
                setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                logmsg(@"controller connected");
                serve(fd, token);
                logmsg(@"controller disconnected");
            }
            if (fd >= 0) close(fd);
        }
        usleep(250 * 1000);
    }
}

__attribute__((constructor)) static void mmbridgeInit(void) {
    pthread_t thread;
    if (pthread_create(&thread, NULL, connectLoop, NULL) == 0) pthread_detach(thread);
}
